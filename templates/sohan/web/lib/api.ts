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

export async function apiPut<T, U>(path: string, body: U): Promise<T> {
  const response = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(`PUT ${path} failed (${response.status}): ${message}`);
  }

  return (await response.json()) as T;
}

export async function apiPatch<T, U>(path: string, body: U): Promise<T> {
  const response = await fetch(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(`PATCH ${path} failed (${response.status}): ${message}`);
  }

  return (await response.json()) as T;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(`DELETE ${path} failed (${response.status}): ${message}`);
  }

  return (await response.json()) as T;
}
