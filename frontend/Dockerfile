# Production Docker image for Next.js application
# Use Node.js 19 as the base image
FROM node:22-alpine AS base

# Install dependencies only when needed
FROM base AS deps
WORKDIR /app

# Add package manager to the path
ENV PATH /app/node_modules/.bin:$PATH

# Install dependencies needed for node-gyp
RUN apk add --no-cache libc6-compat

# Copy package files
COPY package.json package-lock.json ./

# Install dependencies with strict CI mode
RUN npm ci

# Rebuild the source code only when needed
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Set environment variables for telemetry
ENV NEXT_TELEMETRY_DISABLED 1

# Set placeholder values for environment variables that will be replaced at runtime
ENV NEXT_PUBLIC_API_PROTOCOL=__NEXT_PUBLIC_API_PROTOCOL__
ENV NEXT_PUBLIC_API_HOSTNAME=__NEXT_PUBLIC_API_HOSTNAME__
ENV NEXT_PUBLIC_API_PORT=__NEXT_PUBLIC_API_PORT__
ENV NEXT_PUBLIC_STORAGE_ACCOUNT_NAME=__NEXT_PUBLIC_STORAGE_ACCOUNT_NAME__
ENV NEXT_PUBLIC_DEFAULT_BRAND=__NEXT_PUBLIC_DEFAULT_BRAND__

# Build the application
RUN npm run build

# Production image, copy all the files and run next
FROM base AS runner
WORKDIR /app

ENV NODE_ENV production
ENV NEXT_TELEMETRY_DISABLED 1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 -G nodejs nextjs

# Copy next.config.js and public folder
COPY --from=builder /app/next.config.ts ./
COPY --from=builder /app/public ./public

# Copy built application
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder /app/replace-env.mjs ./replace-env.mjs

RUN chown -R root:nodejs /app && \
    chmod -R u=rwX,g=rX,o= /app && \
    find /app/.next -type f -name '*.js' -exec sh -c 'for asset do if grep -q "__NEXT_PUBLIC_" "$asset"; then chmod g+w "$asset"; fi; done' sh {} + && \
    mkdir -p /app/.next/cache && \
    chown -R nextjs:nodejs /app/.next/cache && \
    chmod 700 /app/.next/cache

# Create entrypoint script that replaces placeholders with actual environment values
RUN echo '#!/bin/sh' > /app/entrypoint.sh && \
    echo 'set -e' >> /app/entrypoint.sh && \
    echo 'echo "Replacing environment variables..."' >> /app/entrypoint.sh && \
    echo 'node /app/replace-env.mjs' >> /app/entrypoint.sh && \
    echo 'echo "Environment variables replaced."' >> /app/entrypoint.sh && \
    echo 'exec "$@"' >> /app/entrypoint.sh && \
    chown root:nodejs /app/entrypoint.sh && \
    chmod 550 /app/entrypoint.sh

USER nextjs

# Expose the port the app will run on
EXPOSE 3000
ENV HOSTNAME="0.0.0.0"

# Start the application with the entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["node", "server.js"] 
