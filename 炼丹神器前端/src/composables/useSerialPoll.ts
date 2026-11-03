import { onBeforeUnmount, onMounted, watch, type WatchSource } from "vue";
export function useSerialPoll(
  enabled: WatchSource<boolean>,
  operation: (signal: AbortSignal) => Promise<void>,
  delay = 7000,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let stopped = false;
  let running = false;
  let allowed = false;
  function cancel() {
    clearTimeout(timer);
    controller?.abort();
  }
  async function tick() {
    if (stopped || running || !allowed || document.hidden) return;
    running = true;
    controller = new AbortController();
    try {
      await operation(controller.signal);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError"))
        console.error(
          "读取任务未完成：",
          error instanceof Error ? error.name : "UnknownError",
        );
    } finally {
      running = false;
      if (!stopped && allowed && !document.hidden)
        timer = setTimeout(tick, delay);
    }
  }
  watch(
    enabled,
    (value) => {
      allowed = value;
      cancel();
      if (value) void tick();
    },
    { immediate: true },
  );
  function visibility() {
    cancel();
    if (!document.hidden) void tick();
  }
  onMounted(() => document.addEventListener("visibilitychange", visibility));
  onBeforeUnmount(() => {
    stopped = true;
    cancel();
    document.removeEventListener("visibilitychange", visibility);
  });
}
