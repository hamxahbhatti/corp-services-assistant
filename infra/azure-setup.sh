#!/usr/bin/env bash
# OPTIONAL — creates the minimal Azure resources to run the same code against Azure OpenAI + Azure AI Search.
# UNTESTED in this repo (written against the az CLI as of 2026). Requires a pay-as-you-go subscription
# (free trials have zero model quota). Estimated cost for a demo: well under US$5.
#   az login && ./infra/azure-setup.sh <resource-group> <region>
# Region note: this demo uses synthetic data, so a Global Standard deployment is acceptable here. The production
# design (see the deck) uses regional Provisioned throughput in UAE North to keep inference in-country.
set -euo pipefail
RG=${1:-rg-corp-assistant-demo}; LOC=${2:-uaenorth}; SUF=$RANDOM
AOAI=aoai-corp-$SUF; SRCH=srch-corp-$SUF; CS=cs-corp-$SUF

az group create -n $RG -l $LOC -o none
az cognitiveservices account create -n $AOAI -g $RG -l $LOC --kind OpenAI --sku S0 --custom-domain $AOAI -o none
az cognitiveservices account deployment create -n $AOAI -g $RG --deployment-name gpt-5-mini \
  --model-name gpt-5-mini --model-version 2025-08-07 --model-format OpenAI --sku-name GlobalStandard --sku-capacity 50 -o none
az cognitiveservices account deployment create -n $AOAI -g $RG --deployment-name text-embedding-3-small \
  --model-name text-embedding-3-small --model-version 1 --model-format OpenAI --sku-name GlobalStandard --sku-capacity 50 -o none
az search service create -n $SRCH -g $RG -l $LOC --sku free -o none
az cognitiveservices account create -n $CS -g $RG -l $LOC --kind ContentSafety --sku F0 --custom-domain $CS -o none

cat <<ENV

# ---- paste into .env ----
LLM_PROVIDER=azure
CHAT_MODEL=gpt-5-mini
EMBED_MODEL=text-embedding-3-small
AZURE_OPENAI_ENDPOINT=$(az cognitiveservices account show -n $AOAI -g $RG --query properties.endpoint -o tsv)
AZURE_OPENAI_API_KEY=$(az cognitiveservices account keys list -n $AOAI -g $RG --query key1 -o tsv)
SEARCH_BACKEND=azure
AZURE_SEARCH_ENDPOINT=https://$SRCH.search.windows.net
AZURE_SEARCH_API_KEY=$(az search admin-key show --service-name $SRCH -g $RG --query primaryKey -o tsv)
AZURE_SEARCH_INDEX=corp-policies
AZURE_CONTENT_SAFETY_ENDPOINT=$(az cognitiveservices account show -n $CS -g $RG --query properties.endpoint -o tsv)
AZURE_CONTENT_SAFETY_KEY=$(az cognitiveservices account keys list -n $CS -g $RG --query key1 -o tsv)
ENV
echo "Then: pip install -r requirements-azure.txt && python -m app.knowledge.ingest --azure && ./scripts/start.sh"
echo "Clean up afterwards: az group delete -n $RG --yes"
