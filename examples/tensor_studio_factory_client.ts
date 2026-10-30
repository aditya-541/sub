/**
 * Server-side Tensor Studio client for the Subnet Factory API.
 * Keep this module in server-only code; never expose the Factory bearer token to a browser.
 */

export type BuildInput = {
  project_id: string;
  prompt: string;
  specification?: Record<string, unknown>;
  required_paths?: string[];
  sdk_target?: string;
};

export type BuildReceipt = {
  task_id: string;
  status: "queued";
  task_url: string;
  events_url: string;
};

export type FactoryEvent = {
  id?: number;
  event: string;
  data: Record<string, unknown>;
};

const factoryUrl = process.env.SUBNET_FACTORY_URL?.replace(/\/$/, "");
const factoryToken = process.env.SUBNET_FACTORY_TOKEN;

function requireConfiguration(): { url: string; token: string } {
  if (!factoryUrl || !factoryToken) {
    throw new Error("Set SUBNET_FACTORY_URL and SUBNET_FACTORY_TOKEN on the Studio server.");
  }
  return { url: factoryUrl, token: factoryToken };
}

async function factoryFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { url, token } = requireConfiguration();
  const headers = new Headers(init.headers);
  headers.set("authorization", `Bearer ${token}`);
  if (init.body) headers.set("content-type", "application/json");
  return fetch(new URL(path, `${url}/`), {
    ...init,
    headers,
    cache: "no-store",
  });
}

export async function startSubnetBuild(input: BuildInput): Promise<BuildReceipt> {
  const response = await factoryFetch("/projects/build", {
    method: "POST",
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(`Factory rejected build request (${response.status}): ${await response.text()}`);
  }
  return (await response.json()) as BuildReceipt;
}

export async function getBuildStatus<T = Record<string, unknown>>(taskId: string): Promise<T> {
  const response = await factoryFetch(`/tasks/${encodeURIComponent(taskId)}`);
  if (!response.ok) throw new Error(`Factory task lookup failed (${response.status}).`);
  return (await response.json()) as T;
}

export async function getGeneratedFiles<T = Record<string, unknown>>(
  projectId: string,
): Promise<T> {
  const response = await factoryFetch(
    `/projects/${encodeURIComponent(projectId)}/files`,
  );
  if (!response.ok) throw new Error(`Factory file lookup failed (${response.status}).`);
  return (await response.json()) as T;
}

/** Consume Factory SSE events; abort this request when the user leaves the build view. */
export async function streamBuildEvents(
  taskId: string,
  onEvent: (event: FactoryEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await factoryFetch(
    `/tasks/${encodeURIComponent(taskId)}/events`,
    { signal },
  );
  if (!response.ok || !response.body) {
    throw new Error(`Factory event stream failed (${response.status}).`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let eventName = "message";
  let eventId: number | undefined;

  const parseBlock = (block: string) => {
    let data = "";
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("id:")) eventId = Number(line.slice(3).trim());
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (data) {
      onEvent({ id: eventId, event: eventName, data: JSON.parse(data) });
      eventName = "message";
      eventId = undefined;
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      const blocks = pending.split(/\r?\n\r?\n/);
      pending = blocks.pop() ?? "";
      for (const block of blocks) parseBlock(block);
      if (done) {
        if (pending.trim()) parseBlock(pending);
        break;
      }
    }
  } finally {
    reader.releaseLock();
  }
}
