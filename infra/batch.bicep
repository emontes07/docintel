targetScope = 'resourceGroup'

param location string = resourceGroup().location
param storageAccountName string
param registryName string
param environmentName string
param backendImage string
param jobName string
param containerName string = 'docintel-batches'
@minValue(1)
@maxValue(32)
@description('Maximum independent batch shards per manual run. Amortized workers must be started with one per-shard QUALITY_SHARD_INDEX.')
param parallelism int = 1
@minValue(60)
@maxValue(7200)
@description('Replica timeout in seconds; aligned to the deployed production job.')
param replicaTimeout int = 7200

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: containerName
  properties: { publicAccess: 'None' }
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${jobName}-identity'
  location: location
}
var blobContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
resource workerStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, identity.id, blobContributor)
  scope: container
  properties: {
    roleDefinitionId: blobContributor
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
module worker './modules/containerAppJob.bicep' = {
  params: {
    location: location
    jobName: jobName
    containerAppEnvId: environment.id
    DOCKER_IMAGE: backendImage
    batchContainer: containerName
    workerIdentityId: identity.id
    workerClientId: identity.properties.clientId
    scheduleEnabled: false
    syntheticAcceptanceOnly: true
    replicaTimeout: replicaTimeout
    parallelism: parallelism
    AZURE_BLOB_SERVICE_URL: storage.properties.primaryEndpoints.blob
    AZURE_STORAGE_ACCOUNT_NAME: storageAccountName
    AZURE_CONTAINER_REGISTRY_ENDPOINT: registry.properties.loginServer
  }
  dependsOn: [workerStorage, pull]
}

output workerName string = worker.outputs.jobName
output batchStorageUrl string = storage.properties.primaryEndpoints.blob
output batchContainer string = containerName
output workerClientId string = identity.properties.clientId