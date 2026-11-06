/** “登录失效”通知：请求层收到 401 时发出，AuthGate 收到后回到登录页。 */
type Listener = () => void;
const listeners = new Set<Listener>();

/** 订阅；返回取消订阅函数。 */
export function onUnauthorized(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function notifyUnauthorized(): void {
  for (const listener of [...listeners]) listener();
}
