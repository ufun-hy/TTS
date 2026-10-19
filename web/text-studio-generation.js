/* Two independent text requests; callers own applying and saving each result. */
const TextStudioGeneration = (() => {
  async function run(items, process) {
    let next = 0, failed = false, firstError;
    async function worker() {
      while (!failed && next < items.length) {
        const index = next++;
        try {
          await process(items[index], index);
        } catch (error) {
          if (!failed) firstError = error;
          failed = true;
        }
      }
    }
    // Drain both workers before allowing a retry, including a successful
    // in-flight sibling's save when the other request fails.
    await Promise.all([worker(), worker()]);
    if (failed) throw firstError;
  }
  return {run};
})();
