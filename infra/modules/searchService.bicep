// Azure AI Search (Microsoft.Search/searchServices)
// Managed identity only — local auth (admin/query keys) is disabled

@description('Name of the Search service (globally unique, lowercase letters, digits, and dashes)')
param searchServiceName string

@description('Location for the Search service')
param location string

@description('SKU for the Search service')
param skuName string = 'basic'

@description('Deploy a new Search service or reference an existing one')
param deployNew bool = true

resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' = if (deployNew) {
  name: searchServiceName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: skuName
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true
  }
}

resource existingSearchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = if (!deployNew) {
  name: searchServiceName
}

output searchServiceName string = searchServiceName
output searchServiceId string = deployNew ? searchService.id : existingSearchService.id
output searchEndpoint string = 'https://${searchServiceName}.search.windows.net'
