/**
 * API service for interacting with the backend API
 */

// API base URL configuration with GitHub Codespaces detection
const API_PROTOCOL = process.env.NEXT_PUBLIC_API_PROTOCOL || 'http';
const API_HOSTNAME = process.env.NEXT_PUBLIC_API_HOSTNAME || 'localhost';
// For GitHub Codespaces, port is part of the hostname, so this might be empty
const API_PORT = process.env.NEXT_PUBLIC_API_PORT || '8000';

// First build temporary base URL with conditional port inclusion
let API_BASE_URL = API_PORT 
  ? `${API_PROTOCOL}://${API_HOSTNAME}:${API_PORT}/api/v1` 
  : `${API_PROTOCOL}://${API_HOSTNAME}/api/v1`;

// Override with direct API URL if provided
if (process.env.NEXT_PUBLIC_API_URL) {
  console.log(`Overriding API URL with NEXT_PUBLIC_API_URL: ${process.env.NEXT_PUBLIC_API_URL}`);
  // Ensure API URL ends with /api/v1
  API_BASE_URL = process.env.NEXT_PUBLIC_API_URL.endsWith('/api/v1') 
    ? process.env.NEXT_PUBLIC_API_URL 
    : `${process.env.NEXT_PUBLIC_API_URL}/api/v1`;
}

// Export the final configured URL
export { API_BASE_URL };

// Log the configured API URL at startup to help debug connection issues
console.log(`API configured with: ${API_BASE_URL}`);
console.log('API environment variables:');
console.log(`- NEXT_PUBLIC_API_URL: ${process.env.NEXT_PUBLIC_API_URL || 'not set'}`);
console.log(`- NEXT_PUBLIC_API_PROTOCOL: ${process.env.NEXT_PUBLIC_API_PROTOCOL || 'not set'}`);
console.log(`- NEXT_PUBLIC_API_HOSTNAME: ${process.env.NEXT_PUBLIC_API_HOSTNAME || 'not set'}`);
console.log(`- NEXT_PUBLIC_API_PORT: ${process.env.NEXT_PUBLIC_API_PORT || 'not set'}`);

// Enable debug mode to log API requests
const API_DEBUG = process.env.NEXT_PUBLIC_DEBUG_MODE === 'true';

// Types for API requests and responses
// Gallery types
export enum MediaType {
  IMAGE = "image",
  VIDEO = "video",
}

export interface GalleryItem {
  id: string;
  name: string;
  media_type: MediaType;
  url: string;
  container: string;
  size: number;
  content_type: string;
  creation_time: string;
  last_modified: string;
  metadata?: Record<string, string>;
}

export interface GalleryResponse {
  success: boolean;
  message: string;
  total: number;
  limit: number;
  offset: number;
  items: GalleryItem[];
  continuation_token?: string;
}

export interface GalleryUploadResponse {
  success: boolean;
  message: string;
  file_id: string;
  blob_name: string;
  container: string;
  url: string;
  size: number;
  content_type: string;
  original_filename: string;
  metadata?: Record<string, string>;
}

/**
 * Interface for video/image metadata
 */
export interface AssetMetadata {
  [key: string]: string | number | boolean | string[] | object | undefined;
  analysis?: {
    summary?: string;
    products?: string;
    tags?: string[];
    feedback?: string;
    analyzed_at?: string;
  };
  has_analysis?: boolean;
}

export interface MetadataUpdateResponse {
  success: boolean;
  message: string;
  updated: boolean;
}

/**
 * Interface for folder hierarchy
 */
export interface FolderHierarchy {
  [folderName: string]: {
    path: string;
    children: FolderHierarchy;
  };
}

/**
 * Fetch images from the gallery
 */
export async function fetchGalleryImages(
  limit: number = 50, 
  offset: number = 0,
  continuationToken?: string,
  prefix?: string,
  folderPath?: string
): Promise<GalleryResponse> {
  // Build query parameters
  const params = new URLSearchParams();
  params.append('limit', String(limit));
  params.append('offset', String(offset));
  if (continuationToken) {
    params.append('continuation_token', continuationToken);
  }
  if (prefix) {
    params.append('prefix', prefix);
  }
  if (folderPath) {
    params.append('folder_path', folderPath);
  }

  const url = `${API_BASE_URL}/gallery/images?${params.toString()}`;
  
  if (API_DEBUG) {
    console.log(`Fetching gallery images`);
    console.log(`GET ${url}`);
  }
  
  try {
    const response = await fetch(url);

    if (API_DEBUG) {
      console.log(`Response status: ${response.status} ${response.statusText}`);
      if (!response.ok) {
        console.error('Error response:', await response.text().catch(() => 'Could not read response text'));
      }
    }

    if (!response.ok) {
      throw new Error(`Failed to fetch gallery images: ${response.status} ${response.statusText}`);
    }

    const data = await response.json();
    
    if (API_DEBUG) {
      console.log('Response data:', data);
    }
    
    return data;
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : 'Unknown error';
    console.error(`Network error when fetching gallery images: ${errorMessage}`);
    throw error;
  }
}

/**
 * Delete an asset from the gallery
 */
export async function deleteGalleryAsset(
  blobName: string, 
  mediaType: MediaType
): Promise<{success: boolean, message: string}> {
  const params = new URLSearchParams();
  params.append('blob_name', blobName);
  params.append('media_type', mediaType);

  const url = `${API_BASE_URL}/gallery/delete?${params.toString()}`;
  
  if (API_DEBUG) {
    console.log(`Deleting gallery asset: ${blobName}`);
    console.log(`DELETE ${url}`);
  }
  
  try {
    const response = await fetch(url, {
      method: 'DELETE'
    });

    if (API_DEBUG) {
      console.log(`Response status: ${response.status} ${response.statusText}`);
      if (!response.ok) {
        console.error('Error response:', await response.text().catch(() => 'Could not read response text'));
      }
    }

    if (!response.ok) {
      throw new Error(`Failed to delete gallery asset: ${response.status} ${response.statusText}`);
    }

    const data = await response.json();
    
    if (API_DEBUG) {
      console.log('Response data:', data);
    }
    
    return data;
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : 'Unknown error';
    console.error(`Network error when deleting gallery asset: ${errorMessage}`);
    throw error;
  }
}

/**
 * Update asset metadata
 */
export async function updateAssetMetadata(
  blobName: string,
  mediaType: MediaType,
  metadata: AssetMetadata
): Promise<MetadataUpdateResponse> {
  // Extract asset ID from blob name (remove extension and folder path)
  const assetId = blobName.split('.')[0].split('/').pop();
  
  const params = new URLSearchParams();
  params.append('media_type', mediaType);
  
  const url = `${API_BASE_URL}/metadata/${assetId}?${params.toString()}`;
  
  if (API_DEBUG) {
    console.log(`Updating metadata for asset: ${assetId} (blob: ${blobName})`);
    console.log(`PUT ${url}`);
    console.log('Metadata:', metadata);
  }
  
  try {
    const response = await fetch(url, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(metadata),  // Send metadata directly, not wrapped
    });
    
    if (API_DEBUG) {
      console.log(`Response status: ${response.status} ${response.statusText}`);
      if (!response.ok) {
        console.error('Error response:', await response.text().catch(() => 'Could not read response text'));
      }
    }
    
    if (!response.ok) {
      throw new Error(`Failed to update metadata: ${response.status} ${response.statusText}`);
    }
    
    const data = await response.json();
    
    if (API_DEBUG) {
      console.log('Response data:', data);
    }
    
    return data;
  } catch (error) {
    console.error('Error updating metadata:', error);
    throw error;
  }
}

/**
 * Fetch folders
 */
export async function fetchFolders(
  mediaType?: MediaType
): Promise<{folders: string[], folder_hierarchy: FolderHierarchy}> {
  let url = `${API_BASE_URL}/gallery/folders`;
  
  if (mediaType) {
    url += `?media_type=${mediaType}`;
  }
  
  if (API_DEBUG) {
    console.log(`Fetching folders`);
    console.log(`GET ${url}`);
  }
  
  try {
    const response = await fetch(url);
    
    if (API_DEBUG) {
      console.log(`Response status: ${response.status} ${response.statusText}`);
      if (!response.ok) {
        console.error('Error response:', await response.text().catch(() => 'Could not read response text'));
      }
    }
    
    if (!response.ok) {
      throw new Error(`Failed to fetch folders: ${response.status} ${response.statusText}`);
    }
    
    const data = await response.json();
    
    if (API_DEBUG) {
      console.log('Folders response data:', data);
    }
    
    // Backend now returns simple string array
    // But keep compatibility check in case of old format
    interface LegacyFolder {
      folder_path?: string;
      id?: string;
    }
    
    const folderPaths = data.folders ? 
      (Array.isArray(data.folders) && data.folders.length > 0 && typeof data.folders[0] === 'string' 
        ? data.folders as string[]
        : data.folders.map((folder: string | LegacyFolder) => 
            typeof folder === 'string' ? folder : folder.folder_path || folder.id || ''
          )
      ) : [];
    
    return {
      folders: folderPaths,
      folder_hierarchy: data.folder_hierarchy || {}
    };
  } catch (error) {
    console.error('Error fetching folders:', error);
    throw error;
  }
}

/**
 * Create a new folder in the gallery
 */
export async function createFolder(
  folderPath: string,
  mediaType: MediaType = MediaType.IMAGE
): Promise<{success: boolean, folder_path: string}> {
  const url = `${API_BASE_URL}/gallery/folders`;
  
  if (API_DEBUG) {
    console.log(`Creating folder: ${folderPath}`);
    console.log(`POST ${url}`);
  }
  
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        folder_path: folderPath,
        media_type: mediaType
      }),
    });
    
    if (API_DEBUG) {
      console.log(`Response status: ${response.status} ${response.statusText}`);
      if (!response.ok) {
        console.error('Error response:', await response.text().catch(() => 'Could not read response text'));
      }
    }
    
    if (!response.ok) {
      throw new Error(`Failed to create folder: ${response.status} ${response.statusText}`);
    }
    
    const data = await response.json();
    
    if (API_DEBUG) {
      console.log('Create folder response data:', data);
    }
    
    return {
      success: data.success,
      folder_path: data.folder_path
    };
  } catch (error) {
    console.error('Error creating folder:', error);
    throw error;
  }
}

/**
 * Move an asset to a different folder
 */
export async function moveAsset(
  blobName: string,
  targetFolder: string,
  mediaType: MediaType
): Promise<{success: boolean, message: string}> {
  const url = `${API_BASE_URL}/gallery/move`;
  
  if (API_DEBUG) {
    console.log(`Moving asset ${blobName} to folder ${targetFolder}`);
    console.log(`PUT ${url}`);
  }
  
  try {
    const response = await fetch(url, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        blob_name: blobName,
        media_type: mediaType,
        target_folder: targetFolder
      }),
    });
    
    if (API_DEBUG) {
      console.log(`Response status: ${response.status} ${response.statusText}`);
      if (!response.ok) {
        console.error('Error response:', await response.text().catch(() => 'Could not read response text'));
      }
    }
    
    if (!response.ok) {
      throw new Error(`Failed to move asset: ${response.status} ${response.statusText}`);
    }
    
    const data = await response.json();
    
    if (API_DEBUG) {
      console.log('Move asset response data:', data);
    }
    
    return {
      success: data.success,
      message: data.message
    };
  } catch (error) {
    console.error('Error moving asset:', error);
    throw error;
  }
}
