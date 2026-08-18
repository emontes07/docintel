// File: infra/main.bicep

// This Bicep template deploys the Visionary Lab infrastructure:
// - Azure AI Foundry (AIServices) with all model deployments
// - Azure Container App Environment with backend and frontend Container Apps
// - Azure Storage Account, Cosmos DB, Container Registry, Log Analytics
// - RBAC role assignments for managed identity access (no API keys)

@description('Location for all resources')
param location string = resourceGroup().location

// Parameters for the Container App Environment and Container Apps
@description('Name of the Container App Environment')
param containerAppEnvName string = 'cae-${environmentName}'
@description('Name of the Container App')
param containerAppNameBackend string = 'ca-backend-${environmentName}'
param containerAppNameFrontend string = 'ca-frontend-${environmentName}'
param logAnalyticsWorkspaceName string = 'log-${environmentName}'

@description('Unique name for the Storage Account (3-24 lowercase letters and numbers)')
param storageAccountName string = 'st${toLower(uniqueString(resourceGroup().id, environmentName))}'

@description('Unique name for the Container Registry (5-50 lowercase letters and numbers)')
param containerRegistryName string = 'cr${toLower(uniqueString(resourceGroup().id, environmentName))}'

// AI Foundry parameters
@description('Name of the AI Foundry resource')
param aiFoundryName string
@description('Name of the AI Foundry project')
param aiProjectName string = '${aiFoundryName}-proj'
@description('Location for AI Foundry (some models are region-specific). East US 2 catalogs gpt-4o 2024-11-20 (GlobalStandard) and sora-2 as of 2026-07. Override with `azd env set AI_FOUNDRY_LOCATION <region>` if needed.')
param aiFoundryLocation string = 'eastus2'

// Model deployment names
@description('Name of the LLM deployment')
param LLM_DEPLOYMENT string = 'gpt-5'

// Model types and versions (for Bicep-managed deployments)
// NOTE: gpt-4o (2024-08-06, 2024-11-20) and gpt-4.1 (2025-04-14) are all in
// deprecating state and cannot be used for new deployments in eastus2.
// gpt-5 2025-08-07 is deployable with GlobalStandard SKU
// (per `az cognitiveservices model list -l eastus2 --query "[?model.name=='gpt-5']"`).
// Announced inference deprecation: 2027-02-06.
param llmModelType string = 'gpt-5'
param llmModelVersion string = '2025-08-07'

// TODO(step 3.2): bulk extraction deployment. Hard gate before extract.py can be
// written — confirm the model name, version, and SKU are actually available in
// the Foundry resource's region first:
//   az cognitiveservices model list -l <aiFoundryLocation> -o table
// param bulkExtractionDeploymentName string = ''
// param bulkExtractionModelType string = ''
// param bulkExtractionModelVersion string = ''

// Azure AI Document Intelligence
@description('Unique name for the Document Intelligence resource')
param docIntelName string = 'di${toLower(uniqueString(resourceGroup().id, environmentName))}'

// Azure AI Search
@description('Unique name for the AI Search service (2-60 lowercase letters, digits, and dashes)')
param searchServiceName string = 'srch${toLower(uniqueString(resourceGroup().id, environmentName))}'
@description('SKU for the AI Search service')
param searchSkuName string = 'basic'

// Container Apps Job for batch extraction runs
@description('Name of the Container Apps Job')
param containerAppJobName string = 'caj-extract-${environmentName}'

// Docker images for the backend and frontend container apps
param DOCKER_IMAGE_BACKEND string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param DOCKER_IMAGE_FRONTEND string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param API_PROTOCOL string = ''
param API_HOSTNAME string = ''
param API_PORT string = ''

@description('Brand/skin the frontend ships with. Must match a brand id in frontend/utils/brands.ts.')
param DEFAULT_BRAND string = 'default'

// Easy Auth for frontend (Microsoft tenant restriction)
@secure()
param AUTH_CLIENT_ID string = ''
@secure()
param AUTH_CLIENT_SECRET string = ''
param AUTH_ISSUER string = ''

// Environment name for azd
param environmentName string = ''

// VNet parameters
@description('Name of the Virtual Network')
param vnetName string = 'vnet-${environmentName}'

// Front Door parameters
@description('Name of the Azure Front Door profile')
param frontDoorName string = 'afd-${environmentName}'

// Custom domain for frontend
@description('Custom domain name for the frontend (e.g., visionary.go-agentic.com). Leave empty to skip.')
param frontendCustomDomain string = ''
@description('Managed certificate resource ID for the custom domain. Leave empty for first-time setup.')
param frontendCertificateId string = ''

// Parameters for Cosmos DB
@description('Region for the Cosmos DB account. Defaults to the global location; override when the primary region lacks Cosmos capacity.')
param cosmosLocation string = location
param cosmosAccountName string = 'cosmos-${environmentName}'
param cosmosDatabaseName string = 'VisionaryLabDB'
param cosmosContainerName string = 'visionarylab'

// ─── Azure Storage Account ───
module storageAccountMod './modules/storageAccount.bicep' = {
  name: 'storageAccountMod'
  params: {
    location: location
    storageAccountName: storageAccountName
    deployNew: true
  }
}

module storageContainerMod './modules/storageAccountContainer.bicep' = {
  name: 'storageContainerMod'
  params: {
    storageAccountName: storageAccountName
    containerName: 'images'
    deployNew: true
  }
  dependsOn: [
    storageAccountMod
  ]
}

// ─── Azure Container Registry ───
module containerRegistryMod './modules/containerRegistry.bicep' = {
  name: 'containerRegistryMod'
  params: {
    location: location
    containerRegistryName: containerRegistryName
    deployNew: true
  }
}

// ─── Azure Cosmos DB ───
var cosmosPrefix = toLower(substring(uniqueString(resourceGroup().id, environmentName), 0, 5))
// Cosmos account names are lowercase-only, 3-44 chars, and cannot end with a hyphen
var cosmosAccountNameTruncated = take(toLower('${cosmosPrefix}-${cosmosAccountName}'), 44)
var cosmosAccountNamePrefixed = endsWith(cosmosAccountNameTruncated, '-')
  ? take(cosmosAccountNameTruncated, 43)
  : cosmosAccountNameTruncated
module cosmosDbMod './modules/cosmosDb.bicep' = {
  name: 'cosmosDbMod'
  params: {
    location: cosmosLocation
    cosmosAccountName: cosmosAccountNamePrefixed
    databaseName: cosmosDatabaseName
    containerName: cosmosContainerName
    subnetId: ''
    deployNew: true
    publicNetworkAccess: 'Disabled'
  }
}

// ─── Virtual Network ───
module vnetMod './modules/virtualNetwork.bicep' = {
  name: 'vnetMod'
  params: {
    location: location
    vnetName: vnetName
  }
}

// ─── Private DNS Zones ───
module blobDnsZoneMod './modules/privateDnsZone.bicep' = {
  name: 'blobDnsZoneMod'
  params: {
    zoneName: 'privatelink.blob.core.windows.net'
    vnetId: vnetMod.outputs.vnetId
  }
}

module cosmosDnsZoneMod './modules/privateDnsZone.bicep' = {
  name: 'cosmosDnsZoneMod'
  params: {
    zoneName: 'privatelink.documents.azure.com'
    vnetId: vnetMod.outputs.vnetId
  }
}

// ─── Private Endpoints ───
module storagePrivateEndpointMod './modules/privateEndpoint.bicep' = {
  name: 'storagePrivateEndpointMod'
  params: {
    location: location
    privateEndpointName: 'pe-storage-${environmentName}'
    subnetId: vnetMod.outputs.privateEndpointsSubnetId
    privateLinkServiceId: storageAccountMod.outputs.storageAccountId
    groupIds: ['blob']
    privateDnsZoneId: blobDnsZoneMod.outputs.privateDnsZoneId
  }
}

module cosmosPrivateEndpointMod './modules/privateEndpoint.bicep' = {
  name: 'cosmosPrivateEndpointMod'
  params: {
    location: location
    privateEndpointName: 'pe-cosmos-${environmentName}'
    subnetId: vnetMod.outputs.privateEndpointsSubnetId
    privateLinkServiceId: cosmosDbMod.outputs.cosmosAccountId
    groupIds: ['Sql']
    privateDnsZoneId: cosmosDnsZoneMod.outputs.privateDnsZoneId
  }
}

// ─── Azure Front Door (CDN with private link to storage) ───
var storageHostName = replace(replace(storageAccountMod.outputs.storageAccountPrimaryEndpoint, 'https://', ''), '/', '')
module frontDoorMod './modules/frontDoor.bicep' = {
  name: 'frontDoorMod'
  params: {
    frontDoorName: frontDoorName
    storageAccountHostName: storageHostName
    storageAccountId: storageAccountMod.outputs.storageAccountId
    storageAccountLocation: location
  }
}

// ─── AI Foundry (replaces separate Azure OpenAI resources) ───
module aiFoundryMod './modules/aiFoundry.bicep' = {
  name: 'aiFoundryMod'
  params: {
    aiFoundryName: aiFoundryName
    location: aiFoundryLocation
    deployNew: true
  }
}

module aiFoundryProjectMod './modules/aiFoundryProject.bicep' = {
  name: 'aiFoundryProjectMod'
  params: {
    aiProjectName: aiProjectName
    aiFoundryName: aiFoundryName
    location: aiFoundryLocation
  }
  dependsOn: [
    aiFoundryMod
  ]
}

// ─── Model Deployments (under AI Foundry) ───
// Chained sequentially — Azure doesn't support parallel deployments on the same account
module llmDeployment './modules/aiFoundryModelDeployment.bicep' = {
  name: 'llmDeployment'
  params: {
    aiFoundryName: aiFoundryName
    deploymentName: LLM_DEPLOYMENT
    modelName: llmModelType
    modelVersion: llmModelVersion
    skuCapacity: 30
  }
  dependsOn: [
    aiFoundryMod
  ]
}

// TODO(step 3.2): chain the bulk extraction deployment after llmDeployment once
// the model name/version has been confirmed available in aiFoundryLocation.
// module bulkExtractionDeployment './modules/aiFoundryModelDeployment.bicep' = if (bulkExtractionDeploymentName != '') {
//   name: 'bulkExtractionDeployment'
//   params: {
//     aiFoundryName: aiFoundryName
//     deploymentName: bulkExtractionDeploymentName
//     modelName: bulkExtractionModelType
//     modelVersion: bulkExtractionModelVersion
//     skuCapacity: 30
//   }
//   dependsOn: [
//     llmDeployment
//   ]
// }

// ─── Azure AI Document Intelligence ───
module documentIntelligenceMod './modules/documentIntelligence.bicep' = {
  name: 'documentIntelligenceMod'
  params: {
    docIntelName: docIntelName
    location: location
    deployNew: true
  }
}

// ─── Azure AI Search ───
module searchServiceMod './modules/searchService.bicep' = {
  name: 'searchServiceMod'
  params: {
    searchServiceName: searchServiceName
    location: location
    skuName: searchSkuName
    deployNew: true
  }
}

// ─── Container App Environment ───
module containerAppEnvMod './modules/containerAppEnv.bicep' = {
  name: 'containerAppEnvMod'
  params: {
    location: location
    containerAppEnvName: containerAppEnvName
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    subnetId: vnetMod.outputs.containerAppsSubnetId
    deployNew: true
  }
}

// ─── Container App: Backend ───
module containerAppBackend './modules/containerApp.bicep' = {
  name: 'containerAppBackend'
  params: {
    location: location
    containerAppName: containerAppNameBackend
    containerAppEnvId: containerAppEnvMod.outputs.containerAppEnvId
    targetPort: 80
    deployNew: true
    AZURE_BLOB_SERVICE_URL: storageAccountMod.outputs.storageAccountPrimaryEndpoint
    AZURE_STORAGE_ACCOUNT_NAME: storageAccountName
    AZURE_BLOB_IMAGE_CONTAINER: 'images'
    CDN_BLOB_URL: 'https://${frontDoorMod.outputs.frontDoorEndpointHostName}'
    DOCKER_IMAGE: DOCKER_IMAGE_BACKEND
    AZURE_CONTAINER_REGISTRY_ENDPOINT: containerRegistryMod.outputs.containerRegistryLoginServer
    AZURE_CONTAINER_REGISTRY_USERNAME: containerRegistryMod.outputs.containerRegistryUsername
    AZURE_CONTAINER_REGISTRY_PASSWORD: containerRegistryMod.outputs.containerRegistryPassword
    AI_FOUNDRY_ENDPOINT: aiFoundryMod.outputs.aiFoundryEndpoint
    LLM_DEPLOYMENT: LLM_DEPLOYMENT
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT: documentIntelligenceMod.outputs.docIntelEndpoint
    AZURE_SEARCH_ENDPOINT: searchServiceMod.outputs.searchEndpoint
    COSMOS_ENDPOINT: cosmosDbMod.outputs.cosmosAccountEndpoint
    COSMOS_DATABASE_NAME: cosmosDbMod.outputs.databaseName
    COSMOS_CONTAINER_NAME: cosmosDbMod.outputs.containerName
    azdServiceName: 'backend'
  }
}

// ─── Container App: Frontend ───
module containerAppFrontend './modules/containerApp.bicep' = {
  name: 'containerAppFrontend'
  params: {
    location: location
    containerAppName: containerAppNameFrontend
    containerAppEnvId: containerAppEnvMod.outputs.containerAppEnvId
    targetPort: 3000
    deployNew: true
    AZURE_BLOB_SERVICE_URL: storageAccountMod.outputs.storageAccountPrimaryEndpoint
    AZURE_STORAGE_ACCOUNT_NAME: storageAccountName
    AZURE_BLOB_IMAGE_CONTAINER: 'images'
    CDN_BLOB_URL: 'https://${frontDoorMod.outputs.frontDoorEndpointHostName}'
    DOCKER_IMAGE: DOCKER_IMAGE_FRONTEND
    AZURE_CONTAINER_REGISTRY_ENDPOINT: containerRegistryMod.outputs.containerRegistryLoginServer
    AZURE_CONTAINER_REGISTRY_USERNAME: containerRegistryMod.outputs.containerRegistryUsername
    AZURE_CONTAINER_REGISTRY_PASSWORD: containerRegistryMod.outputs.containerRegistryPassword
    AI_FOUNDRY_ENDPOINT: aiFoundryMod.outputs.aiFoundryEndpoint
    LLM_DEPLOYMENT: LLM_DEPLOYMENT
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT: documentIntelligenceMod.outputs.docIntelEndpoint
    AZURE_SEARCH_ENDPOINT: searchServiceMod.outputs.searchEndpoint
    API_PROTOCOL: API_PROTOCOL == '' ? 'https' : API_PROTOCOL
    API_PORT: API_PORT == '' ? '443' : API_PORT
    API_HOSTNAME: API_HOSTNAME == '' ? '${containerAppNameBackend}.${containerAppEnvMod.outputs.containerAppDefaultDomain}' : API_HOSTNAME
    DEFAULT_BRAND: DEFAULT_BRAND
    enableAuth: AUTH_CLIENT_ID != ''
    authClientId: AUTH_CLIENT_ID
    authClientSecret: AUTH_CLIENT_SECRET
    authIssuer: AUTH_ISSUER
    customDomainName: frontendCustomDomain
    certificateId: frontendCertificateId
    azdServiceName: 'frontend'
  }
}

// ─── Container Apps Job: batch extraction ───
module containerAppJobMod './modules/containerAppJob.bicep' = {
  name: 'containerAppJobMod'
  params: {
    location: location
    jobName: containerAppJobName
    containerAppEnvId: containerAppEnvMod.outputs.containerAppEnvId
    DOCKER_IMAGE: DOCKER_IMAGE_BACKEND
    deployNew: true
    AZURE_CONTAINER_REGISTRY_ENDPOINT: containerRegistryMod.outputs.containerRegistryLoginServer
    AZURE_CONTAINER_REGISTRY_USERNAME: containerRegistryMod.outputs.containerRegistryUsername
    AZURE_CONTAINER_REGISTRY_PASSWORD: containerRegistryMod.outputs.containerRegistryPassword
    AI_FOUNDRY_ENDPOINT: aiFoundryMod.outputs.aiFoundryEndpoint
    LLM_DEPLOYMENT: LLM_DEPLOYMENT
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT: documentIntelligenceMod.outputs.docIntelEndpoint
    AZURE_SEARCH_ENDPOINT: searchServiceMod.outputs.searchEndpoint
    AZURE_BLOB_SERVICE_URL: storageAccountMod.outputs.storageAccountPrimaryEndpoint
    AZURE_STORAGE_ACCOUNT_NAME: storageAccountName
    AZURE_BLOB_IMAGE_CONTAINER: 'images'
    COSMOS_ENDPOINT: cosmosDbMod.outputs.cosmosAccountEndpoint
    COSMOS_DATABASE_NAME: cosmosDbMod.outputs.databaseName
    COSMOS_CONTAINER_NAME: cosmosDbMod.outputs.containerName
  }
}

// ─── RBAC Role Assignments ───

module cosmosRoleAssignmentMod './modules/cosmosRoleAssignment.bicep' = {
  name: 'cosmosRoleAssignmentMod'
  params: {
    cosmosAccountName: cosmosAccountNamePrefixed
    containerAppPrincipalId: containerAppBackend.outputs.containerAppPrincipalId
    dataContributorRoleId: cosmosDbMod.outputs.dataContributorRoleId
  }
}

module aiFoundryRoleAssignmentMod './modules/aiFoundryRoleAssignment.bicep' = {
  name: 'aiFoundryRoleAssignmentMod'
  params: {
    aiFoundryId: aiFoundryMod.outputs.aiFoundryId
    aiFoundryName: aiFoundryName
    containerAppPrincipalId: containerAppBackend.outputs.containerAppPrincipalId
  }
}

module storageRoleAssignmentMod './modules/storageRoleAssignment.bicep' = {
  name: 'storageRoleAssignmentMod'
  params: {
    storageAccountName: storageAccountName
    containerAppPrincipalId: containerAppBackend.outputs.containerAppPrincipalId
  }
}

module docIntelRoleAssignmentMod './modules/documentIntelligenceRoleAssignment.bicep' = {
  name: 'docIntelRoleAssignmentMod'
  params: {
    docIntelName: documentIntelligenceMod.outputs.docIntelName
    principalId: containerAppBackend.outputs.containerAppPrincipalId
  }
}

module searchRoleAssignmentMod './modules/searchRoleAssignment.bicep' = {
  name: 'searchRoleAssignmentMod'
  params: {
    searchServiceName: searchServiceMod.outputs.searchServiceName
    principalId: containerAppBackend.outputs.containerAppPrincipalId
  }
}

// ─── RBAC Role Assignments: extraction job identity ───
// The job has its own system-assigned identity, so every grant the backend app
// has must be repeated for the job's principal.

module jobCosmosRoleAssignmentMod './modules/cosmosRoleAssignment.bicep' = {
  name: 'jobCosmosRoleAssignmentMod'
  params: {
    cosmosAccountName: cosmosAccountNamePrefixed
    containerAppPrincipalId: containerAppJobMod.outputs.jobPrincipalId
    dataContributorRoleId: cosmosDbMod.outputs.dataContributorRoleId
  }
  dependsOn: [
    cosmosRoleAssignmentMod
  ]
}

module jobAiFoundryRoleAssignmentMod './modules/aiFoundryRoleAssignment.bicep' = {
  name: 'jobAiFoundryRoleAssignmentMod'
  params: {
    aiFoundryId: aiFoundryMod.outputs.aiFoundryId
    aiFoundryName: aiFoundryName
    containerAppPrincipalId: containerAppJobMod.outputs.jobPrincipalId
  }
}

module jobStorageRoleAssignmentMod './modules/storageRoleAssignment.bicep' = {
  name: 'jobStorageRoleAssignmentMod'
  params: {
    storageAccountName: storageAccountName
    containerAppPrincipalId: containerAppJobMod.outputs.jobPrincipalId
  }
}

module jobDocIntelRoleAssignmentMod './modules/documentIntelligenceRoleAssignment.bicep' = {
  name: 'jobDocIntelRoleAssignmentMod'
  params: {
    docIntelName: documentIntelligenceMod.outputs.docIntelName
    principalId: containerAppJobMod.outputs.jobPrincipalId
  }
}

module jobSearchRoleAssignmentMod './modules/searchRoleAssignment.bicep' = {
  name: 'jobSearchRoleAssignmentMod'
  params: {
    searchServiceName: searchServiceMod.outputs.searchServiceName
    principalId: containerAppJobMod.outputs.jobPrincipalId
  }
}

// ─── Outputs ───
output AZURE_LOCATION string = location
output AZURE_CONTAINER_ENVIRONMENT_NAME string = containerAppEnvMod.outputs.containerAppEnvId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistryMod.outputs.containerRegistryLoginServer
output BACKEND_URI string = 'https://${containerAppBackend.outputs.containerAppFqdn}'
output BACKEND_INTERNAL_URI string = 'https://${containerAppBackend.outputs.containerAppFqdn}'
output FRONTEND_URI string = 'https://${containerAppFrontend.outputs.containerAppFqdn}'
output AZURE_STORAGE_ACCOUNT_NAME string = storageAccountName
output AZURE_BLOB_SERVICE_URL string = storageAccountMod.outputs.storageAccountPrimaryEndpoint
output AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT string = documentIntelligenceMod.outputs.docIntelEndpoint
output AZURE_SEARCH_ENDPOINT string = searchServiceMod.outputs.searchEndpoint
output AZURE_EXTRACTION_JOB_NAME string = containerAppJobMod.outputs.jobName
output AI_FOUNDRY_ENDPOINT string = aiFoundryMod.outputs.aiFoundryEndpoint
output AI_FOUNDRY_NAME string = aiFoundryName
output COSMOS_DB_ENDPOINT string = cosmosDbMod.outputs.cosmosAccountEndpoint
output COSMOS_DB_DATABASE_NAME string = cosmosDbMod.outputs.databaseName
output COSMOS_DB_CONTAINER_NAME string = cosmosDbMod.outputs.containerName
output CDN_BLOB_URL string = 'https://${frontDoorMod.outputs.frontDoorEndpointHostName}'
