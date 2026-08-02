import "server-only";

import { auth, clerkClient } from "@clerk/nextjs/server";
import type { User } from "@clerk/nextjs/server";

/**
 * The server-side authentication boundary.
 *
 * `server-only` is the first line: it turns "someone imported the secret-key
 * module into a client component" from a runtime secret leak into a build
 * failure. Nothing in here may be re-exported from a `"use client"` file.
 *
 * Every helper fails closed. When Clerk is not configured, or when a lookup
 * errors, the answer is "no user" - never "trust the caller".
 */

export interface AuthedUser {
  id: string;
  email: string | null;
  name: string | null;
  /** Milliseconds since epoch, as Clerk reports it. */
  createdAt: number | null;
}

/**
 * Whether this deployment can actually authenticate anyone.
 *
 * Both halves are required. A publishable key alone renders Clerk's UI but
 * cannot verify a session server-side, and treating that as "configured" would
 * put a login form in front of a boundary that cannot enforce anything.
 */
export function isAuthConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY && process.env.CLERK_SECRET_KEY,
  );
}

/** The signed-in user's ID, or null. Never throws. */
export async function getCurrentUserId(): Promise<string | null> {
  if (!isAuthConfigured()) return null;
  try {
    const { userId } = await auth();
    return userId ?? null;
  } catch {
    // `auth()` throws when clerkMiddleware did not run for this request. That is
    // a configuration fault, and the safe reading of it is "not signed in".
    return null;
  }
}

function primaryEmail(user: User): string | null {
  const primary = user.emailAddresses.find(
    (address) => address.id === user.primaryEmailAddressId,
  );
  return primary?.emailAddress ?? user.emailAddresses[0]?.emailAddress ?? null;
}

/**
 * The signed-in user, or null.
 *
 * Deliberately narrow: an ID, an email, a display name and a creation time.
 * Returning Clerk's full `User` from here would make it trivially easy to render
 * a field the privacy policy does not describe.
 */
export async function getCurrentUser(): Promise<AuthedUser | null> {
  const userId = await getCurrentUserId();
  if (!userId) return null;
  try {
    const client = await clerkClient();
    const user = await client.users.getUser(userId);
    const name = [user.firstName, user.lastName].filter(Boolean).join(" ").trim();
    return {
      id: user.id,
      email: primaryEmail(user),
      name: name.length > 0 ? name : null,
      createdAt: user.createdAt ?? null,
    };
  } catch {
    return null;
  }
}

/**
 * Read a user's private metadata.
 *
 * Private metadata is server-readable only - it is never included in the session
 * token or exposed to the browser - which is what makes it usable as the
 * entitlement cache described in `lib/billing/entitlement.ts`.
 */
export async function getPrivateMetadata(
  userId: string,
): Promise<Record<string, unknown>> {
  if (!isAuthConfigured()) return {};
  try {
    const client = await clerkClient();
    const user = await client.users.getUser(userId);
    return (user.privateMetadata ?? {}) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * Merge keys into a user's private metadata.
 *
 * Clerk's `updateUserMetadata` shallow-merges, so callers pass only the keys
 * they own. Returns whether the write landed: a caller that cannot persist an
 * entitlement must not report success.
 */
export async function mergePrivateMetadata(
  userId: string,
  patch: Record<string, unknown>,
): Promise<boolean> {
  if (!isAuthConfigured()) return false;
  try {
    const client = await clerkClient();
    await client.users.updateUserMetadata(userId, { privateMetadata: patch });
    return true;
  } catch {
    return false;
  }
}

/**
 * Confirm a Clerk user exists.
 *
 * The webhook resolves its subject from Stripe metadata, which is attacker-proof
 * (only this server writes it) but not existence-checked. A subscription whose
 * metadata names a deleted user must not create an entitlement record.
 */
export async function userExists(userId: string): Promise<boolean> {
  if (!isAuthConfigured()) return false;
  try {
    const client = await clerkClient();
    await client.users.getUser(userId);
    return true;
  } catch {
    return false;
  }
}
