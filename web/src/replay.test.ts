import { describe, expect, it, vi } from "vitest";
import { replay } from "./api";

const evts = [{ type: "a", ts: 100 }, { type: "b", ts: 101 }, { type: "c", ts: 109 }];

describe("replay", () => {
  it("instant 同步全量", () => {
    const got: string[] = [];
    replay(evts, (e) => got.push(e.type), { mode: "instant" });
    expect(got).toEqual(["a", "b", "c"]);
  });
  it("paced 等比压缩时序", () => {
    vi.useFakeTimers();
    const got: string[] = [];
    replay(evts, (e) => got.push(e.type), { mode: "paced", targetMs: 900 });
    vi.advanceTimersByTime(100);   // span 9s→900ms：b 在 100ms
    expect(got).toEqual(["a", "b"]);
    vi.advanceTimersByTime(800);
    expect(got).toEqual(["a", "b", "c"]);
    vi.useRealTimers();
  });
  it("stop 停止后续派发", () => {
    vi.useFakeTimers();
    const got: string[] = [];
    const r = replay(evts, (e) => got.push(e.type), { mode: "paced", targetMs: 900 });
    r.stop();
    vi.advanceTimersByTime(2000);
    expect(got).toEqual(["a"]);    // 首事件立即派发，其余被 stop
    vi.useRealTimers();
  });
});
