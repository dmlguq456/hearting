#!/usr/bin/env node
/* Read-only Hearting bridge over Cairn W3a's runtime contracts. */
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const REQUIRED_COMMIT = "1fa0d99e4b714b5ce305f78c8f7c7773255e8f87";
const FORBIDDEN_KEY = /(^|[_-])(write|switch|apply|activate|deactivate|ingest|migrat(?:e|ion))($|[_-])/i;
const FORBIDDEN_EXACT = new Set(["__proto__", "prototype", "constructor", "db_url", "database_url", "credential"]);

type ErrorModule = {
  READ_ERROR_CODES: readonly string[];
  EXIT_CODES: Record<string, number>;
  ReadContractError: new (payload: Record<string, unknown>) => Error;
  errorPayload: (code: string, detail: string) => Record<string, unknown>;
};

function writeObject(value: unknown): void {
  const object = value !== null && typeof value === "object" && !Array.isArray(value)
    ? value : { error: { code: "INTERNAL_FAILURE", detail: "Cairn read returned an invalid response" } };
  process.stdout.write(`${JSON.stringify(object)}\n`);
}

type BootstrapCode = "INVALID_REQUEST" | "INTERNAL_FAILURE";

const BOOTSTRAP_EXIT_CODES: Record<BootstrapCode, number> = {
  INVALID_REQUEST: 4,
  INTERNAL_FAILURE: 18,
};

function bootstrapFailure(code: BootstrapCode, detail: string): never {
  writeObject({ error: { code, detail, retryable: false, observed_at: new Date().toISOString() } });
  process.exit(BOOTSTRAP_EXIT_CODES[code]);
}

function contractFailure(errors: ErrorModule, code: string, detail: string): never {
  const selected = errors.READ_ERROR_CODES.includes(code) ? code : "INTERNAL_FAILURE";
  writeObject({ error: errors.errorPayload(selected, detail) });
  process.exit(errors.EXIT_CODES[selected]);
}

function containsForbiddenOption(value: unknown, seen = new Set<object>()): boolean {
  if (value === null || typeof value !== "object") return false;
  if (seen.has(value)) return true;
  seen.add(value);
  if (Array.isArray(value)) return value.some((item) => containsForbiddenOption(item, seen));
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const normalized = key.toLowerCase();
    if (FORBIDDEN_EXACT.has(normalized) || FORBIDDEN_KEY.test(normalized)) return true;
    if (containsForbiddenOption(child, seen)) return true;
  }
  return false;
}

function verifyErrorModule(value: unknown): asserts value is ErrorModule {
  const errors = value as ErrorModule;
  const codes = errors?.READ_ERROR_CODES;
  if (!Array.isArray(codes) || codes.length !== 17 || new Set(codes).size !== 17
      || typeof errors.errorPayload !== "function" || typeof errors.ReadContractError !== "function"
      || !errors.EXIT_CODES || codes.some((code, index) => errors.EXIT_CODES[code] !== index + 2)) {
    bootstrapFailure("INTERNAL_FAILURE", "Cairn W3a error contract is invalid");
  }
}

async function main(): Promise<void> {
  if (process.argv.length !== 2) bootstrapFailure("INVALID_REQUEST", "command options are not supported");
  const rootValue = process.env.CAIRN_ROOT;
  if (!rootValue) bootstrapFailure("INTERNAL_FAILURE", "Cairn W3a checkout is unavailable");
  const root = path.resolve(rootValue);
  const base = path.join(root, "lib", "artifact-projection", "read");
  const clientPath = path.join(base, "client.ts");
  const errorsPath = path.join(base, "errors.ts");
  const requestPath = path.join(base, "request.ts");
  if (![root, clientPath, errorsPath, requestPath].every((candidate) => {
    try { return existsSync(candidate) && (candidate === root ? statSync(candidate).isDirectory() : statSync(candidate).isFile()); }
    catch { return false; }
  })) bootstrapFailure("INTERNAL_FAILURE", "Cairn W3a checkout is unavailable");

  try {
    execFileSync("git", ["-C", root, "merge-base", "--is-ancestor", REQUIRED_COMMIT, "HEAD"], { stdio: "ignore" });
  } catch {
    bootstrapFailure("INTERNAL_FAILURE", "Cairn W3a revision is not integrated");
  }

  let errors: ErrorModule;
  let clientModule: any;
  let requestModule: any;
  try {
    errors = await import(pathToFileURL(errorsPath).href) as ErrorModule;
    verifyErrorModule(errors);
    clientModule = await import(pathToFileURL(clientPath).href);
    requestModule = await import(pathToFileURL(requestPath).href);
    if (typeof clientModule.HttpReadTransport !== "function"
        || typeof clientModule.ArtifactProjectionClient !== "function"
        || typeof requestModule.validateReadOptions !== "function") {
      contractFailure(errors, "INTERNAL_FAILURE", "Cairn W3a client contract is invalid");
    }
  } catch (error) {
    if ((error as any)?.payload) throw error;
    bootstrapFailure("INTERNAL_FAILURE", "Cairn W3a modules cannot be loaded");
  }

  let request: unknown;
  try {
    request = JSON.parse(readFileSync(0, "utf8"));
  } catch {
    contractFailure(errors, "INVALID_REQUEST", "stdin must contain exactly one JSON object");
  }
  if (request === null || typeof request !== "object" || Array.isArray(request)) {
    contractFailure(errors, "INVALID_REQUEST", "request must be one JSON object");
  }
  if (containsForbiddenOption(request)) {
    contractFailure(errors, "INVALID_REQUEST", "request contains a forbidden non-read option");
  }

  let readRequest: unknown;
  try {
    readRequest = requestModule.validateReadOptions(request);
  } catch {
    contractFailure(errors, "INVALID_REQUEST", "request does not satisfy the Cairn W3a read contract");
  }

  const endpointValue = process.env.CAIRN_READ_ENDPOINT;
  if (!endpointValue) contractFailure(errors, "INTERNAL_FAILURE", "Cairn read endpoint is unavailable");
  try {
    const endpoint = new URL(endpointValue);
    if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password) throw new Error();
  } catch {
    contractFailure(errors, "INTERNAL_FAILURE", "Cairn read endpoint is invalid");
  }

  const fetcher = async (url: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const headers = new Headers(init?.headers);
    const token = process.env.CAIRN_READ_TOKEN;
    if (token) headers.set("authorization", `Bearer ${token}`);
    let response: Response;
    try {
      response = await fetch(url, { ...init, headers });
    } catch {
      throw new errors.ReadContractError(errors.errorPayload("INTERNAL_FAILURE", "Cairn read transport failed"));
    }
    if (!response.ok) {
      let code = "INTERNAL_FAILURE";
      try {
        const body = await response.clone().json() as Record<string, unknown>;
        if (typeof body?.code === "string" && errors.READ_ERROR_CODES.includes(body.code)) code = body.code;
      } catch { /* Response bodies are deliberately absent from diagnostics. */ }
      throw new errors.ReadContractError(errors.errorPayload(code, "Cairn read rejected the request"));
    }
    return response;
  };

  try {
    const transport = new clientModule.HttpReadTransport(endpointValue, fetcher);
    const response = await new clientModule.ArtifactProjectionClient(transport).read(readRequest as any);
    if (response === null || typeof response !== "object" || Array.isArray(response)) {
      contractFailure(errors, "INTERNAL_FAILURE", "Cairn read returned an invalid response");
    }
    writeObject(response);
  } catch (error: any) {
    const payload = error?.payload;
    if (payload && typeof payload.code === "string" && errors.READ_ERROR_CODES.includes(payload.code)) {
      writeObject({ error: payload });
      process.exit(errors.EXIT_CODES[payload.code]);
    }
    contractFailure(errors, "INTERNAL_FAILURE", "Cairn read failed");
  }
}

main().catch(() => bootstrapFailure("INTERNAL_FAILURE", "Cairn W3a bridge failed closed"));
