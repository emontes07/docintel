import NextAuth from "next-auth";
import MicrosoftEntraID from "@auth/core/providers/microsoft-entra-id";

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    MicrosoftEntraID({
      clientId: process.env.AUTH_MICROSOFT_ENTRA_ID_ID,
      issuer: process.env.AUTH_MICROSOFT_ENTRA_ID_TENANT_ID,
      clientSecret: process.env.AUTH_MICROSOFT_ENTRA_ID_SECRET,
      authorization: {
        params: {
          scope: `openid profile email ${process.env.DOCINTEL_API_SCOPE || ""}`,
        },
      },
    }),
  ],
  secret: process.env.AUTH_SECRET,
  callbacks: {
    async jwt({ token, account }) {
      if (account) {
        token.batchAccessToken = account.access_token;
        token.batchExpiresAt = account.expires_at;
      }
      return token;
    },
  },
  // Trust localhost and development URLs
  trustHost: true,
  // If you want to add custom pages, you can do so here:
  // pages: {
  //   signIn: '/login', // Custom login page
  //   // error: '/auth/error', // Error code passed in query string as ?error=
  // },
}); 