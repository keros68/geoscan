// Bridge to the Python JSONL engine host, via the Tauri shell.
//
// Requests carry an id and resolve a Promise when the matching response line
// arrives; id-less lines are events fanned out to subscribers.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

type EventHandler = (data: any) => void;

let nextId = 1;
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void }>();
const handlers = new Map<string, Set<EventHandler>>();
let initialized = false;

export function onEngine(event: string, handler: EventHandler): () => void {
  let set = handlers.get(event);
  if (!set) {
    set = new Set();
    handlers.set(event, set);
  }
  set.add(handler);
  return () => set!.delete(handler);
}

function dispatchEvent(name: string, data: any) {
  handlers.get(name)?.forEach((handler) => handler(data));
}

export async function initEngine(): Promise<void> {
  if (initialized) return;
  initialized = true;
  try {
    await initEngineInner();
  } catch (error) {
    initialized = false; // allow a retry if listener registration failed
    throw error;
  }
}

async function initEngineInner(): Promise<void> {
  await listen<string>("engine-message", (event) => {
    let payload: any;
    try {
      payload = JSON.parse(event.payload);
    } catch {
      return;
    }
    if (payload.id != null && pending.has(payload.id)) {
      const waiter = pending.get(payload.id)!;
      pending.delete(payload.id);
      if (payload.ok) waiter.resolve(payload.data);
      else waiter.reject(new Error(String(payload.error ?? "引擎返回未知错误")));
      return;
    }
    if (payload.event) dispatchEvent(payload.event, payload.data ?? {});
  });

  await listen<string>("engine-stderr", (event) => {
    dispatchEvent("engine_stderr", { message: event.payload });
  });

  await listen("engine-exit", () => {
    dispatchEvent("engine_exit", {});
    pending.forEach((waiter) => waiter.reject(new Error("引擎进程已退出")));
    pending.clear();
  });

  await invoke("engine_start");
}

export async function restartEngine(): Promise<void> {
  // engine_restart kills an alive-but-hung engine before respawning;
  // engine_start alone would be a no-op in that state.
  pending.forEach((waiter) => waiter.reject(new Error("引擎正在重启")));
  pending.clear();
  await invoke("engine_restart");
}

export function engineCall<T = any>(cmd: string, args: Record<string, any> = {}): Promise<T> {
  const id = nextId++;
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve, reject });
    invoke("engine_send", { line: JSON.stringify({ id, cmd, args }) }).catch((error) => {
      pending.delete(id);
      reject(new Error(String(error)));
    });
  });
}
