// Container Apps Job for batch fan-out extraction runs.
// Reuses the backend container image; triggered manually, not on a schedule.

param location string
param jobName string
param containerAppEnvId string
param DOCKER_IMAGE string
param deployNew bool = true

@description('Seconds a replica may run before it is terminated')
param replicaTimeout int = 1800

@description('Number of replicas to start per manual invocation')
param parallelism int = 1

// AI Foundry endpoint (unified for all AI services)
param AI_FOUNDRY_ENDPOINT string = ''
param LLM_DEPLOYMENT string = 'gpt-5'

// Azure AI Document Intelligence and Azure AI Search
param AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT string = ''
param AZURE_SEARCH_ENDPOINT string = ''

// Azure Blob Storage (managed identity — no keys)
param AZURE_BLOB_SERVICE_URL string
param AZURE_STORAGE_ACCOUNT_NAME string
param AZURE_BLOB_IMAGE_CONTAINER string = 'images'

// Cosmos DB (managed identity — no keys)
param COSMOS_ENDPOINT string = ''
param COSMOS_DATABASE_NAME string = ''
param COSMOS_CONTAINER_NAME string = ''

// Azure Container Registry
param AZURE_CONTAINER_REGISTRY_ENDPOINT string = ''
@secure()
param AZURE_CONTAINER_REGISTRY_USERNAME string = ''
@secure()
param AZURE_CONTAINER_REGISTRY_PASSWORD string = ''

resource containerAppJob 'Microsoft.App/jobs@2024-03-01' = if (deployNew) {
  name: jobName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: containerAppEnvId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: replicaTimeout
      replicaRetryLimit: 1
      manualTriggerConfig: {
        parallelism: parallelism
        replicaCompletionCount: parallelism
      }
      registries: AZURE_CONTAINER_REGISTRY_ENDPOINT != '' ? [
        {
          server: AZURE_CONTAINER_REGISTRY_ENDPOINT
          username: AZURE_CONTAINER_REGISTRY_USERNAME
          passwordSecretRef: 'acr-password'
        }
      ] : []
      secrets: AZURE_CONTAINER_REGISTRY_ENDPOINT != '' ? [
        {
          name: 'acr-password'
          value: AZURE_CONTAINER_REGISTRY_PASSWORD
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: jobName
          image: DOCKER_IMAGE
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          env: [
            {
              name: 'AI_FOUNDRY_ENDPOINT'
              value: AI_FOUNDRY_ENDPOINT
            }
            {
              name: 'LLM_DEPLOYMENT'
              value: LLM_DEPLOYMENT
            }
            {
              name: 'AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT'
              value: AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
            }
            {
              name: 'AZURE_SEARCH_ENDPOINT'
              value: AZURE_SEARCH_ENDPOINT
            }
            {
              name: 'AZURE_BLOB_SERVICE_URL'
              value: AZURE_BLOB_SERVICE_URL
            }
            {
              name: 'AZURE_STORAGE_ACCOUNT_NAME'
              value: AZURE_STORAGE_ACCOUNT_NAME
            }
            {
              name: 'AZURE_BLOB_IMAGE_CONTAINER'
              value: AZURE_BLOB_IMAGE_CONTAINER
            }
            {
              name: 'AZURE_COSMOS_DB_ENDPOINT'
              value: COSMOS_ENDPOINT
            }
            {
              name: 'AZURE_COSMOS_DB_ID'
              value: COSMOS_DATABASE_NAME
            }
            {
              name: 'AZURE_COSMOS_CONTAINER_ID'
              value: COSMOS_CONTAINER_NAME
            }
            {
              name: 'AZURE_CONTAINER_REGISTRY_ENDPOINT'
              value: AZURE_CONTAINER_REGISTRY_ENDPOINT
            }
          ]
        }
      ]
    }
  }
}

output jobId string = containerAppJob.id
output jobName string = jobName
output jobPrincipalId string = deployNew ? containerAppJob.identity.principalId : ''
