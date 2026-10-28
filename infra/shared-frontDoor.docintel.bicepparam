using './modules/frontDoor.bicep'

param frontDoorName = 'afd-sora-video-dev-erik2'
param createProfile = false
param endpointName = 'docintel-dev-erik3-ep'
param originGroupName = 'docintel-storage-origin-group'
param originName = 'docintel-storage-blob-origin'
param storageAccountHostName = 'stc4ryis6hullf4.blob.core.windows.net'
param storageAccountId = '/subscriptions/41ff069d-9e57-40a1-971f-710630f3d6bc/resourceGroups/rg-docintel-dev-erik3/providers/Microsoft.Storage/storageAccounts/stc4ryis6hullf4'
param storageAccountLocation = 'eastus'
