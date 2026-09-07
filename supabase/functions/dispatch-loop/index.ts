// Externí dispatcher: pg_cron → tahle funkce → GitHub workflow_dispatch.
// Token jen z env. Nikdy do logu.

const ALLOWED_WORKFLOWS = new Set(["buyer.yml", "exit-orchestrator.yml"]);
const GITHUB_API_VERSION = "2022-11-28";

type DispatchBody = {
  workflow?: string;
  workflows?: string[];
};

type DispatchResult = {
  workflow: string;
  status: number;
  ok: boolean;
};

function envOrThrow(name: string): string {
  const value = Deno.env.get(name)?.trim();
  if (!value) {
    throw new Error(`Chybí ${name} v prostředí funkce.`);
  }
  return value;
}

function requestedWorkflows(body: DispatchBody): string[] {
  const fromEnv = (Deno.env.get("GH_DISPATCH_WORKFLOWS") || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const listed = body.workflows ?? (body.workflow ? [body.workflow] : fromEnv);
  const names = listed.length > 0 ? listed : ["buyer.yml"];
  const unknown = names.filter((name) => !ALLOWED_WORKFLOWS.has(name));
  if (unknown.length > 0) {
    throw new Error(`Workflow mimo allowlist: ${unknown.join(",")}`);
  }
  return names;
}

async function dispatchWorkflow(workflow: string): Promise<DispatchResult> {
  const token = envOrThrow("GH_DISPATCH_TOKEN");
  const owner = Deno.env.get("GH_OWNER")?.trim() || "Thomson512";
  const repo = Deno.env.get("GH_REPO")?.trim() || "InvestAI";
  const ref = Deno.env.get("GH_DISPATCH_REF")?.trim() || "main";
  const url =
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": GITHUB_API_VERSION,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref }),
  });
  const result = { workflow, status: response.status, ok: response.status === 204 };
  console.log(
    `dispatch workflow=${workflow} repo=${owner}/${repo} ref=${ref} status=${result.status} ok=${result.ok}`,
  );
  await response.body?.cancel();
  return result;
}

Deno.serve(async (request) => {
  if (request.method !== "POST") {
    return new Response(JSON.stringify({ error: "POST only" }), {
      status: 405,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const raw = await request.text();
    const body = (raw ? JSON.parse(raw) : {}) as DispatchBody;
    const workflows = requestedWorkflows(body);
    const results: DispatchResult[] = [];
    for (const workflow of workflows) {
      results.push(await dispatchWorkflow(workflow));
    }
    const ok = results.every((item) => item.ok);
    return new Response(JSON.stringify({ ok, results }), {
      status: ok ? 200 : 502,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "dispatch failed";
    console.log(`dispatch error=${message}`);
    return new Response(JSON.stringify({ ok: false, error: message }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});
