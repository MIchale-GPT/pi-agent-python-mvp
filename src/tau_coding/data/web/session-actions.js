(function registerTauSessionActions(global) {
  "use strict";

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

  global.TauSessionActions = Object.freeze({ createAndEnterSession });
})(globalThis);
