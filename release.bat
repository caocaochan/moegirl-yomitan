@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
cd /d "%ROOT%" || exit /b 1

where git >nul 2>nul
if errorlevel 1 (
    echo ERROR: git was not found on PATH.
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python was not found on PATH.
    exit /b 1
)

where gh >nul 2>nul
if errorlevel 1 (
    echo ERROR: GitHub CLI was not found on PATH. Install gh and run "gh auth login".
    exit /b 1
)

echo Fetching release tags...
git fetch --force --tags
if errorlevel 1 exit /b 1

echo Refreshing Moegirl cache...
python -m moegirl_yomitan fetch --retry-attempts 8 --request-timeout 240 --backoff-base-seconds 2
if errorlevel 1 exit /b 1

set "CHANGE_FILE=%TEMP%\moegirl-yomitan-build-change-%RANDOM%-%RANDOM%.txt"

echo Checking packaged content changes...
python -m moegirl_yomitan check-build-change > "%CHANGE_FILE%"
set "CHECK_EXIT=%ERRORLEVEL%"
type "%CHANGE_FILE%"
if not "%CHECK_EXIT%"=="0" (
    del "%CHANGE_FILE%" >nul 2>nul
    exit /b %CHECK_EXIT%
)

for /f "usebackq tokens=1,* delims==" %%A in ("%CHANGE_FILE%") do (
    if "%%A"=="changed" set "CHANGED=%%B"
    if "%%A"=="fingerprint" set "FINGERPRINT=%%B"
)
del "%CHANGE_FILE%" >nul 2>nul

if /i not "%CHANGED%"=="true" (
    echo No packaged content changes detected. No release needed.
    exit /b 0
)

if "%~1"=="" (
    for /f "usebackq delims=" %%V in (`python -c "from moegirl_yomitan.versioning import resolve_build_version; print(resolve_build_version())"`) do (
        set "BUILD_VERSION=%%V"
    )
) else (
    set "BUILD_VERSION=%~1"
)

if not defined BUILD_VERSION (
    echo ERROR: Failed to resolve build version.
    exit /b 1
)

echo Building version %BUILD_VERSION%...
set "MOEGIRL_YOMITAN_BUILD_VERSION=%BUILD_VERSION%"
python -m moegirl_yomitan build --from-cache --output dist/moegirl-yomitan.zip
if errorlevel 1 exit /b 1

if not exist "dist\moegirl-yomitan.zip" (
    echo ERROR: dist\moegirl-yomitan.zip was not created.
    exit /b 1
)

if not exist "dist\moegirl-yomitan-index.json" (
    echo ERROR: dist\moegirl-yomitan-index.json was not created.
    exit /b 1
)

echo Creating GitHub release %BUILD_VERSION%...
gh release create "%BUILD_VERSION%" "dist\moegirl-yomitan.zip" "dist\moegirl-yomitan-index.json" --title "%BUILD_VERSION%" --notes "Manual Yomitan dictionary build for version %BUILD_VERSION%."
if errorlevel 1 exit /b 1

echo Saving released build state...
python -m moegirl_yomitan save-build-state --fingerprint "%FINGERPRINT%"
if errorlevel 1 exit /b 1

echo Release %BUILD_VERSION% published.
exit /b 0
