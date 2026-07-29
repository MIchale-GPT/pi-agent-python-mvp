(function registerTauSessionActions(global) {
  "use strict";

  function resolveTemperature(mode, customValue, capability) {
    if (!capability?.supported || mode === "auto") return null;
    if (mode === "precise") return capability.min;
    if (mode !== "custom") throw new Error(`Unknown temperature mode: ${mode}`);

    const temperature = Number(customValue);
    if (
      customValue === "" ||
      !Number.isFinite(temperature) ||
      temperature < capability.min ||
      temperature > capability.max
    ) {
      throw new Error(
        `Temperature must be between ${capability.min} and ${capability.max}`,
      );
    }
    return temperature;
  }

  async function createAndEnterSession(options, services) {
    const payload = await services.createSession(options);
    const session = payload?.session;
    if (!session || typeof session.id !== "string" || !session.id) {
      throw new Error("Session creation returned an invalid session");
    }
    services.registerSession(session);
    await services.enterSession(session.id);
    return session;
  }

  global.TauSessionActions = Object.freeze({
    createAndEnterSession,
    resolveTemperature,
  });
})(globalThis);
