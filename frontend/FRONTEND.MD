# Frontend Documentation

This document provides a factual overview of the frontend system for DocIntel based on code examination.

> Status: mid-migration. The content generation routes have been removed and the
> document-intelligence UI is not built yet. See
> [MIGRATION-NOTES.md](../MIGRATION-NOTES.md).

## 1. Project Structure

The frontend is a Next.js application with the following key directories:

```
frontend/
├── .env.local               # Local environment variables
├── Dockerfile               # Container configuration
├── app/                     # Next.js App Router pages and layouts
│   ├── api/                 # API routes (Next.js server-side endpoints)
│   │   ├── auth/            # NextAuth handler
│   │   └── environment/     # Environment status route
│   ├── login/               # Login page
│   ├── settings/            # Application settings
│   ├── test-simple/         # Scratch page
│   ├── layout.tsx           # Root layout component
│   ├── not-found.tsx        # 404 handler
│   └── page.tsx             # Landing placeholder for the extraction UI
├── components/              # Reusable React components
│   ├── ui/                  # UI component library
│   ├── app-sidebar.tsx      # Navigation shell
│   ├── page-header.tsx
│   ├── brand-*.tsx          # Brand theming components
│   └── ...                  # Auth, theme, and layout components
├── context/                 # React context providers
├── hooks/                   # Custom React hooks
├── public/                  # Static assets
├── services/                # API service integrations
│   ├── api.ts               # Backend API client (gallery, folders, metadata)
│   └── sas-token.ts         # Azure storage SAS token management
└── utils/                   # Utility functions
```

## 2. Technology Stack

The frontend is built with:

- **Next.js 15.2.4**: App Router architecture with React 19
- **TypeScript**: For type safety
- **Tailwind CSS**: For styling
- **NextAuth.js (next-auth v5)**: For authentication
- **Radix UI**: For accessible UI components
- **Framer Motion**: For animations

## 3. Key Features & Implementation Details

### Authentication

- Uses NextAuth.js with Microsoft Entra ID (formerly Azure AD) provider
- Authentication is defined in `auth.ts` with the following scopes:
  - `openid profile email User.Read`
- The auth system uses environment variables for configuration:
  - `AUTH_MICROSOFT_ENTRA_ID_ID`
  - `AUTH_MICROSOFT_ENTRA_ID_TENANT_ID`
  - `AUTH_MICROSOFT_ENTRA_ID_SECRET`
  - `AUTH_SECRET`

### API Communication

- Backend API is accessed via service modules in `services/` directory
- `api.ts` contains the core API client functionality with:
  - Configurable API URL via environment variables (`NEXT_PUBLIC_API_PROTOCOL`, `NEXT_PUBLIC_API_HOSTNAME`, `NEXT_PUBLIC_API_PORT`)
  - Default URL: `http://localhost:8000/api/v1`
  - Clients for the gallery, folder, and metadata endpoints

### Application UI

- **Landing page**: Placeholder at `/`, to be replaced by the extraction UI
- **Settings**: Available at `/settings`, including environment status
- **Login**: Available at `/login`

### Azure Storage Integration

- Configured to handle assets from Azure Blob Storage
- `next.config.ts` includes remotePatterns for Azure Blob Storage URLs
- SAS token management in `services/sas-token.ts` for secure access to blob storage

### Image Optimization

- Next.js Image component configured in `next.config.ts` for optimized image delivery
- Remote patterns include:
  - Backend API assets (`/api/v1/gallery/asset/**`)
  - Azure Blob Storage (`${STORAGE_ACCOUNT_NAME}.blob.core.windows.net`)

## 4. Environment Configuration

The frontend requires the following environment variables:

- **API Configuration**:
  - `NEXT_PUBLIC_API_PROTOCOL`: Protocol for backend API (http/https)
  - `NEXT_PUBLIC_API_HOSTNAME`: Hostname for backend API
  - `NEXT_PUBLIC_API_PORT`: Port for backend API
  
- **Azure Storage**:
  - `NEXT_PUBLIC_STORAGE_ACCOUNT_NAME`: Azure Storage account name

- **Authentication**:
  - `AUTH_MICROSOFT_ENTRA_ID_ID`: Microsoft Entra ID client ID
  - `AUTH_MICROSOFT_ENTRA_ID_TENANT_ID`: Microsoft Entra ID tenant ID
  - `AUTH_MICROSOFT_ENTRA_ID_SECRET`: Microsoft Entra ID client secret
  - `AUTH_SECRET`: Secret for NextAuth.js

## 5. Development

- The project uses modern Next.js development features:
  - Turbopack for faster development builds (`next dev --turbopack`)
  - Standalone output for optimized production deployment
  - ESLint for code quality enforcement

## 6. Component Architecture

- **UI Components**: Leverages Radix UI primitives for accessible UI components
- **Page Components**: Follows Next.js App Router pattern with:
  - Layout components for shared UI
  - Page components for route-specific content
  - Loading and error states
- **Shell Components**: Shared chrome lives in `app-sidebar.tsx`, `page-header.tsx`, and `animated-layout.tsx`
