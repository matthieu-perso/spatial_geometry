#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Generate Word Embeddings Script
-------------------------------
This script processes sentences and generates word-level embeddings aligned with
linguistic properties like part of speech and dependencies.
"""

import os
import json
import pickle
import argparse
import gc
import sys
from datetime import datetime
from tqdm import tqdm

import torch
import nltk
import stanza
import pandas as pd
import numpy as np
from nltk.corpus import brown
from transformers import AutoTokenizer, AutoModel


def download_required_resources():
    """Download all required resources for NLTK and Stanza."""
    print("Downloading required resources...", flush=True)
    
    # Download NLTK resources
    try:
        nltk.download('brown', quiet=True)
        print("NLTK resources downloaded successfully", flush=True)
    except Exception as e:
        print(f"Error downloading NLTK resources: {e}", flush=True)
    
    # Download Stanza resources
    try:
        stanza.download('en', quiet=True)
        print("Stanza resources downloaded successfully", flush=True)
    except Exception as e:
        print(f"Error downloading Stanza resources: {e}", flush=True)


def reconstruct_sentence(tokens):
    """Reconstruct a sentence from tokens."""
    sentence = " ".join(tokens)
    sentence = sentence.replace('``', '').replace("''", "").replace(
        " ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!")
    return sentence


def get_word_embeddings_aligned(sentence, tokenizer, model, nlp, device=None):
    """
    Given a sentence, aligns subword embeddings from transformer model to words using char offsets from Stanza.
    Returns a list of dicts with word, embedding, POS, dependency, and position.
    """
    doc = nlp(sentence)
    word_spans = [(word.text, word.start_char, word.end_char, word.upos, word.deprel) 
                  for sent in doc.sentences for word in sent.words]

    # Tokenize with offset mapping, no special tokens
    encoding = tokenizer(
        sentence,
        return_offsets_mapping=True,
        return_tensors="pt",
        add_special_tokens=False
    )
    
    # Move to device if specified
    if device is not None:
        encoding = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in encoding.items()}
        
    offsets = encoding["offset_mapping"][0].tolist()
    input_ids = encoding["input_ids"]

    # Get subword embeddings
    with torch.no_grad():
        # Filter out non-tensor values and offset_mapping which is only used for alignment
        # This preserves all model-specific parameters while avoiding type errors
        model_inputs = {k: v for k, v in encoding.items() 
                       if k != 'offset_mapping' and isinstance(v, torch.Tensor)}
            
        output = model(**model_inputs)
        subword_embeddings = output.last_hidden_state.squeeze(0)  # [seq_len, dim]

    # Align subwords to words
    aligned_data = []
    for i, (word, w_start, w_end, upos, deprel) in enumerate(word_spans):
        matching_sub_idxs = [j for j, (s, e) in enumerate(offsets) if s < w_end and e > w_start and s != e]

        if matching_sub_idxs:
            embs = [subword_embeddings[j] for j in matching_sub_idxs]
            word_embedding = torch.stack(embs).mean(dim=0)
            # Move embedding to CPU to save GPU memory
            aligned_data.append({
                "word": word,
                "embedding": word_embedding.cpu(),
                "pos": upos,
                "dep": deprel,
                "position": i
            })

    return aligned_data


def process_sentence_batch(sentences, tokenizer, model, nlp, stanza_docs, device=None):
    """
    Process a batch of sentences to get word embeddings.
    Uses cached Stanza docs when available.
    """
    all_aligned_data = []
    
    for i, sent in enumerate(sentences):
        try:
            # Use cached Stanza doc if available
            if sent in stanza_docs:
                doc = stanza_docs[sent]
                # Use the doc directly with the aligned function
                word_spans = [(word.text, word.start_char, word.end_char, word.upos, word.deprel) 
                              for sent_obj in doc.sentences for word in sent_obj.words]
                
                # Tokenize with offset mapping
                encoding = tokenizer(
                    sent,
                    return_offsets_mapping=True,
                    return_tensors="pt",
                    add_special_tokens=False
                )
                
                # Move to device if specified
                if device is not None:
                    encoding = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in encoding.items()}
                    
                offsets = encoding["offset_mapping"][0].tolist()
                
                # Get subword embeddings
                with torch.no_grad():
                    # Filter out non-tensor values and offset_mapping
                    model_inputs = {k: v for k, v in encoding.items() 
                                  if k != 'offset_mapping' and isinstance(v, torch.Tensor)}
                    
                    output = model(**model_inputs)
                    subword_embeddings = output.last_hidden_state.squeeze(0)  # [seq_len, dim]
                
                # Align subwords to words
                aligned_data = []
                for j, (word, w_start, w_end, upos, deprel) in enumerate(word_spans):
                    matching_sub_idxs = [k for k, (s, e) in enumerate(offsets) if s < w_end and e > w_start and s != e]
                    
                    if matching_sub_idxs:
                        embs = [subword_embeddings[k] for k in matching_sub_idxs]
                        word_embedding = torch.stack(embs).mean(dim=0)
                        # Move embedding to CPU to save GPU memory
                        aligned_data.append({
                            "word": word,
                            "embedding": word_embedding.cpu(),
                            "pos": upos,
                            "dep": deprel,
                            "position": j,
                            "sentence_id": i,
                            "sentence": sent
                        })
                
                all_aligned_data.extend(aligned_data)
            else:
                # Fallback to using nlp directly if not in cache
                aligned = get_word_embeddings_aligned(sent, tokenizer, model, nlp, device)
                for row in aligned:
                    row["sentence_id"] = i
                    row["sentence"] = sent
                    all_aligned_data.append(row)
        except Exception as e:
            print(f"Error processing sentence: {e}")
            continue
    
    # Force garbage collection to free memory
    if device is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()
    
    return all_aligned_data


def process_and_cache_stanza(sentences, cache_path="./stanza_cache.pkl"):
    """
    Process sentences with Stanza and cache the results.
    If cache exists, load from cache instead of reprocessing.
    
    Args:
        sentences: List of sentences to process
        cache_path: Path to save/load cache file
        
    Returns:
        Stanza pipeline and processed documents dictionary
    """
    # Load stanza pipeline
    print("Loading Stanza pipeline...")
    try:
        nlp = stanza.Pipeline('en', processors='tokenize,pos,depparse,lemma')
    except Exception as e:
        print(f"Error loading Stanza pipeline: {e}")
        print("Trying to download resources again...")
        download_required_resources()
        nlp = stanza.Pipeline('en', processors='tokenize,pos,depparse,lemma')
    
    # Check if cache exists
    if os.path.exists(cache_path):
        print(f"Loading Stanza results from cache: {cache_path}")
        with open(cache_path, 'rb') as f:
            stanza_docs = pickle.load(f)
        print(f"Loaded {len(stanza_docs)} processed sentences from cache")
        return nlp, stanza_docs
    
    # Process sentences with Stanza
    print("Processing sentences with Stanza (this may take a while)...")
    stanza_docs = {}
    for sentence in tqdm(sentences, desc="Stanza processing"):
        try:
            doc = nlp(sentence)
            stanza_docs[sentence] = doc
        except Exception as e:
            print(f"Error processing sentence with Stanza: {sentence[:50]}...")
            print(f"Error details: {e}")
    
    # Save to cache
    print(f"Saving Stanza results to cache: {cache_path}")
    with open(cache_path, 'wb') as f:
        pickle.dump(stanza_docs, f)
    
    return nlp, stanza_docs


def main(args):
    # Setup output directory with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.dirname(args.output_path)
    os.makedirs(output_dir, exist_ok=True)
    print(f"Created output directory: {output_dir}", flush=True)
    
    print(f"Using model: {args.model_name}", flush=True)
    
    # Load transformer model and tokenizer
    print(f"Loading model: {args.model_name}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    model = AutoModel.from_pretrained(args.model_name, trust_remote_code=True)
    model.eval()
    print(f"Successfully loaded model and tokenizer", flush=True)
    
    # Set device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    print(f"Using device: {device}", flush=True)
    
    # Get sentences from source
    if args.source == 'brown':
        print("Getting sentences from Brown corpus...", flush=True)
        sentences = [reconstruct_sentence(tokens) for tokens in brown.sents()]
        # Always limit to at most 20,000 sentences, regardless of args.num_sentences
        max_sentences = min(20000, args.num_sentences if args.num_sentences > 0 else 20000)
        sentences = sentences[:max_sentences]
        print(f"Selected {len(sentences)} sentences from Brown corpus (max 20,000)", flush=True)
    else:
        print(f"Reading sentences from file: {args.source}", flush=True)
        with open(args.source, 'r', encoding='utf-8') as f:
            sentences = [line.strip() for line in f if line.strip()]
        # Always limit to at most 20,000 sentences, regardless of args.num_sentences
        max_sentences = min(20000, args.num_sentences if args.num_sentences > 0 else 20000)
        sentences = sentences[:max_sentences]
        print(f"Read {len(sentences)} sentences from file (max 20,000)", flush=True)
    
    # Process and cache Stanza results
    stanza_cache_path = args.stanza_cache_path
    print(f"Using Stanza cache path: {stanza_cache_path}", flush=True)
    nlp, stanza_docs = process_and_cache_stanza(sentences, stanza_cache_path)
    
    print(f"Processing {len(sentences)} sentences with batch size {args.batch_size}...", flush=True)
    
    # Process sentences in batches to manage memory
    all_rows = []
    batch_size = args.batch_size
    num_batches = (len(sentences) + batch_size - 1) // batch_size  # Ceiling division
    
    print(f"Will process sentences in {num_batches} batches", flush=True)
    
    for batch_idx in tqdm(range(num_batches), desc="Processing batches", mininterval=1.0):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(sentences))
        sentence_batch = sentences[start_idx:end_idx]
        
        print(f"Processing batch {batch_idx+1}/{num_batches}: sentences {start_idx} to {end_idx-1}", flush=True)
        
        # Process batch using cached Stanza docs
        batch_rows = process_sentence_batch(sentence_batch, tokenizer, model, nlp, stanza_docs, device)
        all_rows.extend(batch_rows)
        
        # Force garbage collection between batches
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            
        print(f"Completed batch {batch_idx+1}/{num_batches} with {len(batch_rows)} word embeddings", flush=True)
    
    # Convert to DataFrame
    print("Creating DataFrame...", flush=True)
    df = pd.DataFrame(all_rows)
    
    if len(df) == 0:
        print("Error: No valid embeddings were generated. Check your input data.", flush=True)
        return
        
    print(f"Created DataFrame with {len(df)} rows", flush=True)
    
    # Create metadata dictionary
    metadata = {
        'model_name': args.model_name,
        'embedding_dim': df['embedding'].iloc[0].shape[0] if len(df) > 0 else None,
        'generation_timestamp': timestamp
    }
    print(f"Created metadata: {metadata}", flush=True)
    
    # Extract embeddings before saving to CSV
    print("Extracting embeddings...", flush=True)
    embeddings = {}
    for idx, row in enumerate(df.itertuples()):
        embeddings[idx] = row.embedding
        if idx % 10000 == 0 and idx > 0:
            print(f"Extracted {idx}/{len(df)} embeddings", flush=True)
    
    # Save embeddings separately
    embeddings_path = args.output_path.replace('.csv', '_embeddings.pkl')
    print(f"Saving embeddings to {embeddings_path}...", flush=True)
    with open(embeddings_path, 'wb') as f:
        pickle.dump(embeddings, f)
    print(f"Successfully saved embeddings for {len(embeddings)} words", flush=True)
    
    # Save metadata separately
    metadata_path = args.output_path.replace('.csv', '_metadata.json')
    print(f"Saving metadata to {metadata_path}...", flush=True)
    with open(metadata_path, 'w') as f:
        json.dump(metadata, f, indent=2)
    print("Successfully saved metadata", flush=True)
    
    # Save model configuration and tokenizer for later use
    model_save_path = args.output_path.replace('.csv', '_model_info.pkl')
    print(f"Saving model info to {model_save_path}...", flush=True)
    with open(model_save_path, 'wb') as f:
        model_info = {
            'model_name': args.model_name,
            'config': model.config
        }
        pickle.dump(model_info, f)
    print("Successfully saved model info", flush=True)
    
    # Remove embedding column before saving to CSV
    print("Preparing CSV data...", flush=True)
    df_csv = df.copy()
    df_csv['embedding_idx'] = range(len(df_csv))  # Add index to link back to embeddings
    df_csv = df_csv.drop(columns=['embedding'])
    print(f"Prepared CSV data with {len(df_csv)} rows", flush=True)
    
    # Save dataframe to CSV
    print(f"Saving CSV data to {args.output_path}...", flush=True)
    df_csv.to_csv(args.output_path, index=False)
    print("Successfully saved CSV data", flush=True)
    
    print(f"Dataset saved to {args.output_path}", flush=True)
    print(f"Embeddings saved to {embeddings_path}", flush=True)
    print(f"Metadata saved to {metadata_path}", flush=True)
    print(f"Model info saved to {model_save_path}", flush=True)
    print(f"Dataset size: {len(df)} words from {len(sentences)} sentences", flush=True)
    print(f"Embedding dimension: {metadata['embedding_dim']}", flush=True)
    
    # Print some statistics
    if len(df) > 0:
        print(f"Number of unique POS tags: {df['pos'].nunique()}", flush=True)
        print(f"Number of unique dependency relations: {df['dep'].nunique()}", flush=True)
        print(f"Vocabulary size: {df['word'].nunique()}", flush=True)
        print(f"Max position: {df['position'].max()}", flush=True)
        
    # Clean up to free memory
    del model, tokenizer, df, df_csv, embeddings
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate word-level embeddings from sentences")
    
    # Model parameters
    parser.add_argument("--model_name", type=str, default="sentence-transformers/all-MiniLM-L6-v2",
                        help="Transformer model name to use")
    
    # Input parameters
    parser.add_argument("--source", type=str, default="brown",
                        help="Source of sentences: 'brown' for Brown corpus or path to a text file")
    parser.add_argument("--num_sentences", type=int, default=20000,
                        help="Maximum number of sentences to process")
    
    # Output parameters
    parser.add_argument("--output_path", type=str, default="./embeddings.csv",
                        help="Path to save the processed embeddings as CSV")
    
    # Processing parameters
    parser.add_argument("--batch_size", type=int, default=32,
                        help="Number of sentences to process in each batch")
    
    # Misc parameters
    parser.add_argument("--download_resources", action="store_true",
                        help="Download required NLTK and Stanza resources")
    parser.add_argument("--verbose", action="store_true",
                        help="Print verbose output")
    parser.add_argument("--stanza_cache_path", type=str, default="./stanza_cache.pkl",
                        help="Path to save/load Stanza processing cache")
    
    args = parser.parse_args()
    main(args)