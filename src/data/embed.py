import torch
from datasets import load_dataset
from tqdm import tqdm
from sentence_transformers import SentenceTransformer

HF_DATASET = "viknat/spatial-geometry"
DATA_DIR = "src/data/sentence-embeddings"

MODELS = [
    "Alibaba-NLP/gte-large-en-v1.5",
    "intfloat/multilingual-e5-large",
    "sentence-transformers/all-mpnet-base-v2",
    "sentence-transformers/all-MiniLM-L6-v2"
]

def get_embeddings_path(model_name):
    return f"{DATA_DIR}/{model_name.replace('/', '_')}.pt"

print('Loading dataset...')
sentences = []
dataset = load_dataset(HF_DATASET, split="train")

for entry in tqdm(dataset):
    sentences.append({
        'sentence': entry['sentence'],
        'relation': entry['relation'],
        'subject': entry['subject'],
        'object': entry['object']
    })
print(f"Loaded {len(sentences)} sentences from dataset")

for model_name in MODELS:
    print(f"Processing with model: {model_name}")
    model = SentenceTransformer(model_name, trust_remote_code=True)
    
    embeddings = model.encode([s['sentence'] for s in sentences])
    print(f"Embeddings shape: {embeddings.shape}")

    data = [{"sentence": s['sentence'], "relation": s['relation'], 
             "subject": s['subject'], "object": s['object'], 
             "embedding": embedding} 
            for s, embedding in zip(sentences, embeddings)]

    torch.save(data, get_embeddings_path(model_name))

    print(f"Saved embeddings to {DATA_DIR}/{model_name.replace('/', '_')}.pt\n")