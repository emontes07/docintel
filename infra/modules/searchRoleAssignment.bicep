// Role assignments for Azure AI Search: grants a managed identity data-plane
// index access and control-plane service access without keys

@description('Name of the Search service')
param searchServiceName string

@description('Principal ID of the managed identity to grant access to')
param principalId string

// Built-in role definition IDs
var searchIndexDataContributorRoleId = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
var searchServiceContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'

resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: searchServiceName
}

// Search Index Data Contributor — read/write documents in indexes
resource indexDataContributorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchService.id, principalId, searchIndexDataContributorRoleId)
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataContributorRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

// Search Service Contributor — manage indexes, indexers, and data sources
resource serviceContributorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchService.id, principalId, searchServiceContributorRoleId)
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchServiceContributorRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

output indexDataContributorRoleAssignmentId string = indexDataContributorRole.id
output serviceContributorRoleAssignmentId string = serviceContributorRole.id
