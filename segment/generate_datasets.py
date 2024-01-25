import os
import math
import torch
import torch.nn as nn

from datasets import load_dataset, Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoModel,
    AutoTokenizer,
    BitsAndBytesConfig,
    HfArgumentParser,
    TrainingArguments,
    pipeline,
    logging,
    TrainerCallback,
)
from peft import LoraConfig, PeftModel
from trl import SFTTrainer

import os
from datasets import load_dataset, Dataset
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
import pandas as pd
import numpy as np
from itertools import islice
from tqdm.auto import tqdm

def preprocess_instruct(examples):
    # Concatenate 'prompt' and 'completion' fields
    texts = [prompt + " " + completion for prompt, completion in zip(examples['prompt'], examples['completion'])]
    return {'text': texts}

model_name = "meta-llama/Llama-2-7b-hf" # Also try "mistralai/Mistral-7B-v0.1"
dataset = "guanaco"
cluster = "cedar"

num_clusters = 2  # Adjust the number of clusters as needed
cluster_strategy = "gradients"
resume_from_cluster = 0

if cluster == "cedar":
    if dataset == "guanaco":
        dataset_name = "timdettmers/openassistant-guanaco"
        train_dataset = load_dataset(dataset_name, split="train")
    elif dataset == "instruct":
        dataset_name = "monology/VMware-open-instruct-higgsfield"
        train_dataset = load_dataset(dataset_name, split="train")
        train_dataset = train_dataset.map(preprocess_instruct, batched=True)
    print(f"Training dataset set to {dataset_name} from hugging face")

if cluster == "narval":
    if dataset == "guanaco":
        dataset_path = "/scratch/alif/timdettmers___json/timdettmers--openassistant-guanaco-c93588435bc90172/0.0.0/fe5dd6ea2639a6df622901539cb550cf8797e5a6b2dd7af1cf934bed8e233e6e/json-train.arrow"
        train_dataset = Dataset.from_file(dataset_path)
    elif dataset == "instruct":
        dataset_path = '/scratch/alif/monology___v_mware-open-instruct-higgsfield/default/0.0.0/622a7cf65a222fcb/v_mware-open-instruct-higgsfield-train.arrow'
        train_dataset = Dataset.from_file(dataset_path)
        train_dataset = train_dataset.map(preprocess_instruct, batched=True)
    print(f"Training dataset set to: {dataset} from local path: {dataset_path}")
    
    
### GRADIENTS ###


def compute_gradients_for_batch(batch, tokenizer, model, max_length=None):
    # Check if tokenizer has a padding token, if not, set it to the eos_token
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    
    # Tokenize the batch
    inputs = tokenizer(batch, return_tensors='pt', padding=True, max_length=max_length, truncation=True)
    inputs = {k: v.to("cuda") for k, v in inputs.items()}
    
    # Forward pass
    outputs = model(**inputs, labels=inputs['input_ids'])
    loss = outputs.loss
    model.zero_grad()
    
    # Backward pass to get gradients
    loss.backward()
    
    # Collect gradients
    gradients = []
    for name, parameter in model.named_parameters():
        if parameter.requires_grad and parameter.grad is not None:
            gradients.append(parameter.grad.detach().cpu().numpy().flatten())
    return np.concatenate(gradients)


#### CLUSTERING #########

def cluster_gradients(dataset, num_clusters, batch_size, max_length):

    # Load the model and tokenizer
    model_name = "meta-llama/Llama-2-7b-hf"  # replace with your model
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        low_cpu_mem_usage=True,
        return_dict=True,
        torch_dtype=torch.float16,
        device_map="auto",
    )

    # Set the model to evaluation mode and to the device
    # device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # model.to(device)
    model.eval()

    # Computing gradients for the dataset in batches
    gradient_features = []
    for i in tqdm(range(0, len(train_dataset), batch_size), desc='Computing gradients'):
        batch_texts = train_dataset['text'][i:i+batch_size]
        batch_gradients = compute_gradients_for_batch(batch_texts, tokenizer, model, max_length)
        gradient_features.append(batch_gradients)
        model.zero_grad()  # Important to clear gradients after processing each batch

    # Convert the list of batch gradient arrays into a single array
    gradient_features = np.vstack(gradient_features)
    
    # Cluster the gradient features
    kmeans = KMeans(n_clusters=num_clusters, random_state=0)
    kmeans.fit(gradient_features)
    cluster_labels = kmeans.labels_

    # Create the clustered dataset
    clustered_data = pd.DataFrame({'text': train_dataset['text'], 'cluster': cluster_labels})
    return clustered_data

def cluster_embeddings(dataset, num_clusters, embeddings):    
    # Extract text data
    texts = [entry['text'] for entry in dataset]
    prompts = [entry['prompt'] for entry in dataset]
    completions = [entry['completion'] for entry in dataset]

    # Clustering
    kmeans = KMeans(n_clusters=num_clusters, random_state=0)
    kmeans.fit(embeddings)

    # Assigning cluster labels to each text
    cluster_labels = kmeans.labels_

    # Creating a DataFrame for easier visualization
    clustered_data = pd.DataFrame({'text': texts, 'prompt': prompts, 'completion': completions, 'cluster': cluster_labels})

    return clustered_data

def cluster_tfid(dataset, num_clusters):    
    # Extract text data (adjust the key 'text' if your dataset has a different text field)
    texts = [entry['text'] for entry in dataset]

    # Data Preparation and Feature Extraction
    vectorizer = TfidfVectorizer(stop_words='english')
    X = vectorizer.fit_transform(texts)

    # Clustering
    kmeans = KMeans(n_clusters=num_clusters, random_state=0)
    kmeans.fit(X)

    # Assigning cluster labels to each text
    cluster_labels = kmeans.labels_

    # Creating a DataFrame for easier visualization
    clustered_data = pd.DataFrame({'text': texts, 'cluster': cluster_labels})

    return clustered_data

if cluster_strategy == "embeddings":
    embeddings = np.load(f'embeddings/{dataset}_embeddings.npy')
    clustered_data = cluster_embeddings(train_dataset, num_clusters, embeddings)
    print(f'Created {num_clusters} clusters from precomputed embeddings file: {dataset}_embeddings.npy')
elif cluster_strategy == "tfid":
    clustered_data = cluster_tfid(train_dataset, num_clusters)
elif cluster_strategy == "gradients":
    batch_size = 6
    max_length = 1024
    clustered_data = cluster_gradients(train_dataset, num_clusters, batch_size, max_length)
    save_path = f"gradient_clusters_{num_clusters}.csv" 
    clustered_data.to_csv(save_path)
    print(f"Clustered data using model gradients and saved dataframe to {save_path}")

unique_clusters = clustered_data['cluster'].unique()
cluster_datasets = {}

# Loop through each cluster and create datasets
for cluster_label in unique_clusters:
    cluster_df = clustered_data[clustered_data['cluster'] == cluster_label]
    cluster_datasets[f"cluster_{cluster_label}"] = Dataset.from_pandas(cluster_df)
    
# Sort the cluster numerically
cluster_datasets = dict(sorted(cluster_datasets.items()))  

count = 1
total = len(cluster_datasets.items())

for cluster_label, cluster_dataset in islice(cluster_datasets.items(), resume_from_cluster, total, 1):   
    
    print(f'({count}/{total}) Saving {cluster_label} for {dataset} dataset...')
    print(cluster_dataset[0])
    dataset_length = len(cluster_dataset)    
    dataset_name = f"data/{dataset}_{num_clusters}_{cluster_label}_{dataset_length}.json"
    cluster_dataset.to_json(dataset_name)
    count += 1
