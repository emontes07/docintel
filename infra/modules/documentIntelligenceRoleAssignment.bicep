// Role assignment: Cognitive Services User on the Document Intelligence resource
// Allows a managed identity to call the Document Intelligence APIs without keys

@description('Name of the Document Intelligence resource')
param docIntelName string

@description('Principal ID of the managed identity to grant access to')
param principalId string

// Cognitive Services User role definition ID
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource docIntelResource 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: docIntelName
}

resource docIntelRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docIntelResource.id, principalId, cognitiveServicesUserRoleId)
  scope: docIntelResource
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

output roleAssignmentId string = docIntelRoleAssignment.id
