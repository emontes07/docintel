// Azure Front Door Premium with private link origin to Storage blob
// Required for browser access to blobs when storage has public access disabled

@description('Name of the Front Door profile')
param frontDoorName string

@description('Create a dedicated Premium profile. Set false when deploying children into an existing shared profile.')
param createProfile bool = true

param endpointName string = '${frontDoorName}-ep'
param originGroupName string = 'storage-origin-group'
param originName string = 'storage-blob-origin'

@description('Hostname of the storage account blob endpoint (e.g., stxxx.blob.core.windows.net)')
param storageAccountHostName string

@description('Resource ID of the storage account')
param storageAccountId string

@description('Location of the storage account (required for private link)')
param storageAccountLocation string

var privateLinkRequestMessage = createProfile
  ? 'Front Door private link to blob storage'
  : 'Front Door private link: ${frontDoorName}/${originGroupName}/${originName}'

resource newFrontDoorProfile 'Microsoft.Cdn/profiles@2024-02-01' = if (createProfile) {
  name: frontDoorName
  location: 'global'
  sku: {
    name: 'Premium_AzureFrontDoor'
  }
  properties: {}
}

resource frontDoorProfile 'Microsoft.Cdn/profiles@2024-02-01' existing = {
  name: frontDoorName
}

resource frontDoorEndpoint 'Microsoft.Cdn/profiles/afdEndpoints@2024-02-01' = {
  parent: frontDoorProfile
  name: endpointName
  location: 'global'
  properties: {
    enabledState: 'Enabled'
  }
  dependsOn: [
    newFrontDoorProfile
  ]
}

resource originGroup 'Microsoft.Cdn/profiles/originGroups@2024-02-01' = {
  parent: frontDoorProfile
  name: originGroupName
  properties: {
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
    }
    healthProbeSettings: null
  }
  dependsOn: [
    newFrontDoorProfile
  ]
}

resource origin 'Microsoft.Cdn/profiles/originGroups/origins@2024-02-01' = {
  parent: originGroup
  name: originName
  properties: {
    hostName: storageAccountHostName
    originHostHeader: storageAccountHostName
    httpPort: 80
    httpsPort: 443
    priority: 1
    weight: 1000
    enforceCertificateNameCheck: true
    sharedPrivateLinkResource: {
      privateLink: {
        id: storageAccountId
      }
      groupId: 'blob'
      privateLinkLocation: storageAccountLocation
      requestMessage: privateLinkRequestMessage
    }
  }
}

resource route 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-02-01' = {
  parent: frontDoorEndpoint
  name: 'storage-route'
  properties: {
    originGroup: {
      id: originGroup.id
    }
    supportedProtocols: [
      'Https'
    ]
    patternsToMatch: [
      '/*'
    ]
    forwardingProtocol: 'HttpsOnly'
    linkToDefaultDomain: 'Enabled'
    httpsRedirect: 'Enabled'
  }
  dependsOn: [
    origin
  ]
}

output frontDoorEndpointHostName string = frontDoorEndpoint.properties.hostName
output frontDoorProfileId string = frontDoorProfile.id
output privateLinkRequestMessage string = privateLinkRequestMessage
