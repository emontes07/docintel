// Azure AI Document Intelligence (Microsoft.CognitiveServices/accounts kind 'FormRecognizer')
// Managed identity only — local auth (API keys) is disabled

@description('Name of the Document Intelligence resource')
param docIntelName string

@description('Location for the Document Intelligence resource')
param location string

@description('Deploy a new Document Intelligence resource or reference an existing one')
param deployNew bool = true

resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2025-06-01' = if (deployNew) {
  name: docIntelName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  kind: 'FormRecognizer'
  properties: {
    customSubDomainName: docIntelName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource existingDocumentIntelligence 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (!deployNew) {
  name: docIntelName
}

output docIntelEndpoint string = deployNew ? documentIntelligence.properties.endpoint : existingDocumentIntelligence.properties.endpoint
output docIntelName string = docIntelName
output docIntelId string = deployNew ? documentIntelligence.id : existingDocumentIntelligence.id
