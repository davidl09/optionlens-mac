// The environment for programs OptionLens starts (helpers, host tools) when it runs as an AppImage: AppRun
// adds the AppImage's mount folder to the search paths, and its bundled libraries (libnotify, libindicator…)
// must not be picked up by host programs, nor stale paths outlive the mount. Elsewhere: unchanged.
/** Search paths that the AppImage's AppRun prefixes with its mount folder ($APPDIR). */
const APPRUN_PATHS = ["PATH", "LD_LIBRARY_PATH", "XDG_DATA_DIRS", "GSETTINGS_SCHEMA_DIR"] as const;

/**
 * The environment without the running AppImage's variables and without its mount folder in the search
 * paths: that mount is gone once the app quits, and a new AppImage sets its own.
 */
export function withoutAppImageEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const { APPIMAGE: _a, APPDIR: appDir, ARGV0: _v, OWD: _o, ...rest } = env;
  if (!appDir) return rest;
  for (const key of APPRUN_PATHS) {
    const kept = (rest[key] ?? "").split(":").filter((p) => p !== "" && p !== appDir && !p.startsWith(`${appDir}/`));
    if (kept.length) rest[key] = kept.join(":"); else delete rest[key];
  }
  return rest;
}
