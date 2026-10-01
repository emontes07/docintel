// Container Apps Job for batch fan-out extraction runs.
// Reuses the backend container image; triggered manually, not on a schedule.

param location string
param jobName string
param containerAppEnvId string
param DOCKER_IMAGE string
param deployNew bool = true
param batchContainer string = ''
param workerIdentityId string = ''
param workerClientId string = ''
param scheduleEnabled bool = false
param syntheticAcceptanceOnly bool = false

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
  identity: empty(workerIdentityId) ? { type: 'SystemAssigned' } : {
    type: 'UserAssigned'
    userAssignedIdentities: { '${workerIdentityId}': {} }
  }
  properties: {
    environmentId: containerAppEnvId
    configuration: {
      triggerType: scheduleEnabled ? 'Schedule' : 'Manual'
      replicaTimeout: replicaTimeout
      replicaRetryLimit: 0
      manualTriggerConfig: scheduleEnabled ? null : {
        parallelism: parallelism
        replicaCompletionCount: parallelism
      }
      scheduleTriggerConfig: scheduleEnabled ? {
        cronExpression: '*/5 * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      } : null
      registries: AZURE_CONTAINER_REGISTRY_ENDPOINT != '' ? [
        {
          server: AZURE_CONTAINER_REGISTRY_ENDPOINT
          identity: empty(workerIdentityId) ? null : workerIdentityId
          username: empty(workerIdentityId) ? AZURE_CONTAINER_REGISTRY_USERNAME : null
          passwordSecretRef: empty(workerIdentityId) ? 'acr-password' : null
        }
      ] : []
      secrets: AZURE_CONTAINER_REGISTRY_ENDPOINT != '' && empty(workerIdentityId) ? [
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
          command: ['/app/.venv/bin/python']
          args: syntheticAcceptanceOnly
            ? ['-m', 'backend.batch_worker', '--concurrency', '2', '--max-batches', '1', '--item-limit', '2', '--synthetic-acceptance']
            : ['-m', 'backend.batch_worker', '--concurrency', '2', '--max-batches', '1', '--item-limit', '100']
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          env: [
            { name: 'AZURE_CLIENT_ID', value: workerClientId }
            { name: 'DOCINTEL_BATCH_STORAGE_URL', value: AZURE_BLOB_SERVICE_URL }
            { name: 'DOCINTEL_BATCH_CONTAINER', value: batchContainer }
            { name: 'DOCINTEL_BATCH_LIVE_ENABLED', value: 'false' }
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
output jobPrincipalId string = deployNew && empty(workerIdentityId) ? (containerAppJob.?identity.?principalId ?? '') : ''
