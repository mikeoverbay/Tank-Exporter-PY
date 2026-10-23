/* ============================================================================
 *  Tank Exporter PY -- one-file Windows installer
 *
 *  tepy_setup.c
 *
 *  SUPERSEDED 2026-10-23 by installer/install.bat + install.ps1.  This
 *  compiles to an UNSIGNED exe that downloads and then runs other
 *  installers -- which is the textbook fingerprint of a trojan
 *  downloader, so Windows Defender quarantines it on sight (it deleted
 *  a built copy off the owner's machine).  Nothing here is wrong; the
 *  behaviour is just unwinnable while the exe is unsigned, so the shipped
 *  installer is now the readable .bat/.ps1 pair, which Defender does not
 *  auto-delete.  Kept only as a reference / a starting point if the exe
 *  is ever code-signed.  DO NOT point users at the exe.
 *
 *  What it does, in order:
 *    1. Find Python (`py -3` launcher first, bare `python` second --
 *       same preference order as go.bat, for the same WindowsApps-stub
 *       reason).  If neither works, download the official python.org
 *       installer and run it with a progress bar (/passive), PATH +
 *       launcher enabled, current-user install.
 *    2. Download the latest project snapshot straight from GitHub
 *       (master branch zip) into %TEMP%.
 *    3. Extract it into the folder the user picked (default
 *       %USERPROFILE%\TankExporterPY) using Windows' built-in tar.exe
 *       (bsdtar, ships with Windows 10 1803+ and handles .zip).
 *       The zip expands to a Tank-Exporter-PY-master\ subfolder.
 *    4. Drop a "Tank Exporter PY" shortcut on the Desktop pointing at
 *       go.bat -- go.bat owns first-run pip install and the launch,
 *       so this installer never duplicates that logic.
 *    5. Offer to launch right away.
 *
 *  Build (needs any Visual Studio with C tools -- see build.bat):
 *      cl /nologo /W3 /O1 /MT tepy_setup.c urlmon.lib wininet.lib
 *  /MT statically links the C runtime so the exe runs on a machine
 *  with nothing installed.
 *
 *  Command line (all optional, prompts when omitted):
 *      tepy_setup.exe [install_dir] [/noshortcut] [/nolaunch] [/yes]
 *      /yes = take every default without asking (unattended).
 * ========================================================================= */

#include <windows.h>
#include <urlmon.h>
#include <wininet.h>
#include <stdio.h>
#include <string.h>
#include <direct.h>

#pragma comment(lib, "urlmon.lib")
#pragma comment(lib, "wininet.lib")
#pragma comment(lib, "shell32.lib")

#define REPO_ZIP_URL \
    "https://github.com/mikeoverbay/Tank-Exporter-PY/archive/refs/heads/master.zip"
#define REPO_SUBDIR   "Tank-Exporter-PY-master"
/* Pinned rather than "latest": a known-good interpreter for the
 * project's wheel set (pygame / PyOpenGL / numpy / Pillow /
 * imgui-bundle all publish 3.13 wheels).  Bump deliberately. */
#define PYTHON_URL \
    "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"
/* Blender powers FBX / GLB / OBJ import + export (headless bridge).
 * 4.2 LTS is the series TEPY is exercised against; pinned to the
 * newest LTS patch.  Optional -- setup never fails over it. */
#define BLENDER_URL \
    "https://download.blender.org/release/Blender4.2/blender-4.2.14-windows-x64.msi"

static int g_yes = 0;   /* /yes: take defaults, never prompt */

/* ---- small helpers ------------------------------------------------------ */

static void say(const char *msg)
{
    printf("%s\n", msg);
    fflush(stdout);
}

static int ask_yesno(const char *question, int default_yes)
{
    char buf[32];
    if (g_yes)
        return default_yes;
    printf("%s [%s]: ", question, default_yes ? "Y/n" : "y/N");
    fflush(stdout);
    if (!fgets(buf, sizeof(buf), stdin))
        return default_yes;
    if (buf[0] == '\n' || buf[0] == '\r')
        return default_yes;
    return (buf[0] == 'y' || buf[0] == 'Y');
}

/* Run a command line, wait for it, return its exit code (-1 = could
 * not start).  Uses cmd.exe so builtins and .bat files work too. */
static int run_wait(const char *cmdline, int show)
{
    char full[2048];
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    DWORD code = (DWORD)-1;

    _snprintf(full, sizeof(full), "cmd.exe /C %s", cmdline);
    full[sizeof(full) - 1] = 0;

    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    if (!show) {
        si.dwFlags = STARTF_USESHOWWINDOW;
        si.wShowWindow = SW_HIDE;
    }
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessA(NULL, full, NULL, NULL, FALSE,
                        0, NULL, NULL, &si, &pi))
        return -1;
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}

/* Download url -> dest.  URLMon serves from the IE cache when it has
 * a hit, so the cache entry is purged first -- otherwise a re-run
 * could install a stale snapshot forever. */
static int download(const char *url, const char *dest, const char *label)
{
    HRESULT hr;
    printf("Downloading %s ...\n  %s\n", label, url);
    fflush(stdout);
    DeleteUrlCacheEntryA(url);
    hr = URLDownloadToFileA(NULL, url, dest, 0, NULL);
    if (hr != S_OK) {
        printf("ERROR: download failed (HRESULT 0x%08lX).\n"
               "Check your internet connection and retry.\n",
               (unsigned long)hr);
        return 0;
    }
    return 1;
}

/* ---- step 1: Python ----------------------------------------------------- */

static const char *find_python(void)
{
    /* Same order as go.bat: `py -3` beats bare `python` because the
     * WindowsApps store stub shadows real installs on most PATHs. */
    if (run_wait("py -3 --version >nul 2>&1", 0) == 0)
        return "py -3";
    if (run_wait("python --version >nul 2>&1", 0) == 0)
        return "python";
    return NULL;
}

static int ensure_python(void)
{
    char tmp[MAX_PATH], exe[MAX_PATH], cmd[1024];

    if (find_python()) {
        say("Python: found.");
        return 1;
    }
    say("Python: NOT found on this machine.");
    if (!ask_yesno("Download and install Python 3.13 now?", 1)) {
        say("Cannot continue without Python.  Install it from "
            "https://www.python.org/downloads/ and re-run.");
        return 0;
    }
    GetTempPathA(sizeof(tmp), tmp);
    _snprintf(exe, sizeof(exe), "%spython-setup.exe", tmp);
    exe[sizeof(exe) - 1] = 0;
    if (!download(PYTHON_URL, exe, "Python 3.13"))
        return 0;

    /* /passive = progress bar, no questions.  Per-user install,
     * PATH prepended, py launcher included (go.bat prefers it). */
    say("Installing Python (a progress window will appear) ...");
    _snprintf(cmd, sizeof(cmd),
              "\"%s\" /passive InstallAllUsers=0 PrependPath=1 "
              "Include_launcher=1", exe);
    cmd[sizeof(cmd) - 1] = 0;
    if (run_wait(cmd, 1) != 0) {
        say("ERROR: the Python installer did not finish cleanly.");
        return 0;
    }
    /* This process inherited the OLD PATH, so `python` may still be
     * invisible here -- but the py launcher installs system-wide
     * app paths and is usually visible immediately.  go.bat runs in
     * a fresh shell later and will see everything either way. */
    if (!find_python())
        say("NOTE: Python installed but not visible to this window "
            "yet.  That's fine -- the launcher shortcut runs in a "
            "fresh shell.");
    else
        say("Python: installed.");
    return 1;
}

/* ---- step 1b: Blender (optional) ---------------------------------------- */

static int find_blender(void)
{
    /* PATH first (rare), then the standard install roots: every
     * Blender MSI lands at
     *   C:\Program Files\Blender Foundation\Blender X.Y\blender.exe
     * -- same locations TEPY's own find_blender_executable scans. */
    static const char *roots[] = {
        "C:\\Program Files\\Blender Foundation",
        "C:\\Program Files (x86)\\Blender Foundation",
    };
    WIN32_FIND_DATAA fd;
    HANDLE h;
    char probe[MAX_PATH], exe[MAX_PATH];
    int i;

    if (run_wait("where blender >nul 2>&1", 0) == 0)
        return 1;
    for (i = 0; i < 2; i++) {
        _snprintf(probe, sizeof(probe), "%s\\*", roots[i]);
        probe[sizeof(probe) - 1] = 0;
        h = FindFirstFileA(probe, &fd);
        if (h == INVALID_HANDLE_VALUE)
            continue;
        do {
            if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
                    && fd.cFileName[0] != '.') {
                _snprintf(exe, sizeof(exe), "%s\\%s\\blender.exe",
                          roots[i], fd.cFileName);
                exe[sizeof(exe) - 1] = 0;
                if (GetFileAttributesA(exe)
                        != INVALID_FILE_ATTRIBUTES) {
                    FindClose(h);
                    return 1;
                }
            }
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    return 0;
}

static void ensure_blender(void)
{
    /* Optional: TEPY views / extracts / Save-Prims fine without
     * Blender -- only FBX / GLB / OBJ import + export need it.  So
     * every failure path here is a warning, never a setup abort. */
    char tmp[MAX_PATH], msi[MAX_PATH], params[1024];
    SHELLEXECUTEINFOA sei;
    DWORD code = 1;

    if (find_blender()) {
        say("Blender: found.");
        return;
    }
    say("Blender: NOT found.  (Only needed for FBX / GLB / OBJ "
        "import + export -- everything else works without it.)");
    if (!ask_yesno("Download and install Blender 4.2 LTS now?", 1)) {
        say("Skipping Blender.  Install it later from blender.org "
            "if you want FBX import/export.");
        return;
    }
    GetTempPathA(sizeof(tmp), tmp);
    _snprintf(msi, sizeof(msi), "%sblender-setup.msi", tmp);
    msi[sizeof(msi) - 1] = 0;
    if (!download(BLENDER_URL, msi, "Blender 4.2 LTS (~350 MB)")) {
        say("WARNING: Blender download failed -- continuing "
            "without it.");
        return;
    }
    /* The MSI is per-machine, so msiexec needs elevation; the
     * "runas" verb pops the normal Windows permission prompt.
     * /qb = unattended with a progress bar. */
    say("Installing Blender (Windows will ask for permission) ...");
    _snprintf(params, sizeof(params), "/i \"%s\" /qb", msi);
    params[sizeof(params) - 1] = 0;
    ZeroMemory(&sei, sizeof(sei));
    sei.cbSize = sizeof(sei);
    sei.fMask = SEE_MASK_NOCLOSEPROCESS;
    sei.lpVerb = "runas";
    sei.lpFile = "msiexec.exe";
    sei.lpParameters = params;
    sei.nShow = SW_SHOWNORMAL;
    if (!ShellExecuteExA(&sei) || !sei.hProcess) {
        say("WARNING: Blender install was declined or failed to "
            "start.  TEPY still works; FBX import/export won't "
            "until Blender is installed.");
        return;
    }
    WaitForSingleObject(sei.hProcess, INFINITE);
    GetExitCodeProcess(sei.hProcess, &code);
    CloseHandle(sei.hProcess);
    DeleteFileA(msi);
    if (code == 0 && find_blender())
        say("Blender: installed.");
    else
        say("WARNING: the Blender installer did not finish cleanly "
            "-- FBX import/export may be unavailable until it's "
            "installed from blender.org.");
}

/* ---- main ---------------------------------------------------------------- */

int main(int argc, char **argv)
{
    char install_dir[MAX_PATH] = "";
    char app_dir[MAX_PATH], tmp[MAX_PATH], zip[MAX_PATH];
    char cmd[2048], gobat[MAX_PATH];
    int make_shortcut = 1, offer_launch = 1;
    int i;

    say("=====================================");
    say("  Tank Exporter PY -- installer");
    say("=====================================");
    say("");

    for (i = 1; i < argc; i++) {
        if (_stricmp(argv[i], "/noshortcut") == 0)
            make_shortcut = 0;
        else if (_stricmp(argv[i], "/nolaunch") == 0)
            offer_launch = 0;
        else if (_stricmp(argv[i], "/yes") == 0)
            g_yes = 1;
        else
            strncpy(install_dir, argv[i], sizeof(install_dir) - 1);
    }

    /* -- install folder -- */
    if (!install_dir[0]) {
        char def[MAX_PATH], buf[MAX_PATH];
        const char *home = getenv("USERPROFILE");
        _snprintf(def, sizeof(def), "%s\\TankExporterPY",
                  home ? home : "C:");
        def[sizeof(def) - 1] = 0;
        if (g_yes) {
            strcpy(install_dir, def);
        } else {
            printf("Install folder [%s]: ", def);
            fflush(stdout);
            if (fgets(buf, sizeof(buf), stdin)) {
                buf[strcspn(buf, "\r\n")] = 0;
                strcpy(install_dir, buf[0] ? buf : def);
            } else {
                strcpy(install_dir, def);
            }
        }
    }
    printf("Installing to: %s\n\n", install_dir);

    /* -- 1. Python -- */
    if (!ensure_python()) {
        say("\nSetup aborted.");
        if (!g_yes) { say("Press Enter to close."); getchar(); }
        return 1;
    }

    /* -- 1b. Blender (optional, for FBX import/export) -- */
    ensure_blender();

    /* -- 2. download the project -- */
    GetTempPathA(sizeof(tmp), tmp);
    _snprintf(zip, sizeof(zip), "%stepy_master.zip", tmp);
    zip[sizeof(zip) - 1] = 0;
    if (!download(REPO_ZIP_URL, zip, "Tank Exporter PY (latest)")) {
        if (!g_yes) { say("Press Enter to close."); getchar(); }
        return 1;
    }

    /* -- 3. extract -- */
    say("Extracting ...");
    _mkdir(install_dir);        /* ok if it already exists */
    _snprintf(cmd, sizeof(cmd), "tar -xf \"%s\" -C \"%s\"",
              zip, install_dir);
    cmd[sizeof(cmd) - 1] = 0;
    if (run_wait(cmd, 0) != 0) {
        say("ERROR: extraction failed.  Is this Windows 10 1803 or "
            "newer?  (The built-in tar.exe is required.)");
        if (!g_yes) { say("Press Enter to close."); getchar(); }
        return 1;
    }
    DeleteFileA(zip);
    _snprintf(app_dir, sizeof(app_dir), "%s\\%s",
              install_dir, REPO_SUBDIR);
    app_dir[sizeof(app_dir) - 1] = 0;
    _snprintf(gobat, sizeof(gobat), "%s\\go.bat", app_dir);
    gobat[sizeof(gobat) - 1] = 0;
    if (GetFileAttributesA(gobat) == INVALID_FILE_ATTRIBUTES) {
        say("ERROR: extraction finished but go.bat is missing -- "
            "the download may be corrupt.  Re-run the installer.");
        if (!g_yes) { say("Press Enter to close."); getchar(); }
        return 1;
    }
    printf("Installed to: %s\n\n", app_dir);

    /* -- 4. desktop shortcut -- */
    if (make_shortcut) {
        /* WScript.Shell via a one-line PowerShell call: the only
         * sane way to write a .lnk without 80 lines of COM in C. */
        _snprintf(cmd, sizeof(cmd),
            "powershell -NoProfile -Command "
            "\"$s=(New-Object -ComObject WScript.Shell)"
            ".CreateShortcut([Environment]::GetFolderPath('Desktop')"
            "+'\\Tank Exporter PY.lnk');"
            "$s.TargetPath='%s';"
            "$s.WorkingDirectory='%s';"
            "$s.Description='Tank Exporter PY';"
            "$s.Save()\"",
            gobat, app_dir);
        cmd[sizeof(cmd) - 1] = 0;
        if (run_wait(cmd, 0) == 0)
            say("Desktop shortcut created: Tank Exporter PY");
        else
            say("WARNING: could not create the desktop shortcut.  "
                "Run go.bat from the install folder instead.");
    }

    say("");
    say("Done.  First launch installs the Python packages it needs");
    say("(one-time, ~a minute) and then opens the viewer.");
    say("In the app, open Set Paths and point it at your");
    say("World of Tanks folder.");
    say("");

    /* -- 5. launch -- */
    if (offer_launch && ask_yesno("Launch Tank Exporter PY now?", 1)) {
        _snprintf(cmd, sizeof(cmd),
                  "start \"Tank Exporter PY\" /D \"%s\" cmd /c go.bat",
                  app_dir);
        cmd[sizeof(cmd) - 1] = 0;
        run_wait(cmd, 1);
    } else if (!g_yes) {
        say("Press Enter to close.");
        getchar();
    }
    return 0;
}
