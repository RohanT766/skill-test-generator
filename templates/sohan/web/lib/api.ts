export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`GET ${path} failed (${response.status})`);
  }

  return (await response.json()) as T;
}

export async function apiPost<T, U>(path: string, body: U): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(`POST ${path} failed (${response.status}): ${message}`);
  }

  return (await response.json()) as T;
}
