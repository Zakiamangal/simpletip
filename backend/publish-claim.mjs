#!/usr/bin/env node
/**
 * Publish a tip claim using the @cooperation/claim-atproto SDK.
 *
 * Called from Python backend via subprocess:
 *   node publish-claim.mjs '{"tip": {...}}'
 *
 * Reads ATProto credentials from env: ATPROTO_HANDLE, ATPROTO_APP_PASSWORD, ATPROTO_SERVICE
 * Outputs JSON: {"uri": "at://...", "cid": "..."} on success
 * Exits non-zero with error message on failure.
 */

import { AtpAgent } from "@atproto/api";
import {
  ClaimClient,
  createClaim,
  createSource,
} from "@cooperation/claim-atproto";

const ATPROTO_HANDLE = process.env.ATPROTO_HANDLE;
const ATPROTO_APP_PASSWORD = process.env.ATPROTO_APP_PASSWORD;
const ATPROTO_SERVICE = process.env.ATPROTO_SERVICE || "https://bsky.social";

if (!ATPROTO_HANDLE || !ATPROTO_APP_PASSWORD) {
  console.error(
    JSON.stringify({ error: "ATPROTO_HANDLE and ATPROTO_APP_PASSWORD required" })
  );
  process.exit(1);
}

// Read tip data from CLI argument
const input = process.argv[2];
if (!input) {
  console.error(JSON.stringify({ error: "Usage: publish-claim.mjs '{...}'" }));
  process.exit(1);
}

let tipData;
try {
  tipData = JSON.parse(input);
} catch (e) {
  console.error(JSON.stringify({ error: `Invalid JSON: ${e.message}` }));
  process.exit(1);
}

async function main() {
  // Authenticate with ATProto
  const agent = new AtpAgent({ service: ATPROTO_SERVICE });
  await agent.login({
    identifier: ATPROTO_HANDLE,
    password: ATPROTO_APP_PASSWORD,
  });

  const client = new ClaimClient({ agent });

  // Build claim using the SDK builders
  const builder = createClaim()
    .subject(tipData.subject)   // tipper DID
    .type("tip")                // claimType
    .confidence(1.0);           // tip is a concrete action

  if (tipData.object) {
    builder.object(tipData.object);           // receiver name
  }
  if (tipData.statement) {
    builder.statement(tipData.statement);     // human-readable
  }
  if (tipData.effectiveDate) {
    builder.effectiveDate(tipData.effectiveDate);
  }
  if (tipData.createdAt) {
    builder.createdAt(tipData.createdAt);
  }

  // Add source (content URL) if provided
  if (tipData.sourceUri) {
    const sourceBuilder = createSource()
      .uri(tipData.sourceUri)
      .howKnown("FIRST_HAND");
    builder.withSource(sourceBuilder);
  }

  const claim = builder.build();

  // Publish using the SDK
  const published = await client.publish(claim);

  // Output result as JSON
  console.log(JSON.stringify({
    uri: published.uri,
    cid: published.cid,
  }));
}

main().catch((err) => {
  console.error(JSON.stringify({ error: err.message }));
  process.exit(1);
});
