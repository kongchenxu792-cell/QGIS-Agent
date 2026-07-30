@echo off
cd /d "%~dp0"

set OSGEO4W_ROOT=%~dp0qgis-portable
set QGIS_PREFIX_PATH=%OSGEO4W_ROOT%\apps\qgis-ltr
set GDAL_FILENAME_IS_UTF8=YES
set PROJ_LIB=%OSGEO4W_ROOT%\share\proj
set GDAL_DATA=%OSGEO4W_ROOT%\apps\gdal\share\gdal
set PROJ_DATA=%OSGEO4W_ROOT%\share\proj
set VSI_CACHE=TRUE
set VSI_CACHE_SIZE=1000000
set QT_PLUGIN_PATH=%OSGEO4W_ROOT%\apps\qgis-ltr\qtplugins;%OSGEO4W_ROOT%\apps\Qt5\plugins
set GDAL_DRIVER_PATH=%OSGEO4W_ROOT%\apps\gdal\lib\gdalplugins

set PYTHONHOME=%OSGEO4W_ROOT%\apps\Python312
set PYTHONUTF8=1

set SSL_CERT_FILE=%OSGEO4W_ROOT%\bin\curl-ca-bundle.crt
set SSL_CERT_DIR=%OSGEO4W_ROOT%\apps\openssl\certs

set PATH=%OSGEO4W_ROOT%\apps\Qt5\bin;%OSGEO4W_ROOT%\bin;%OSGEO4W_ROOT%\apps\Python312;%OSGEO4W_ROOT%\apps\Python312\Scripts;%OSGEO4W_ROOT%\apps\qgis-ltr\bin;C:\WINDOWS\system32;C:\WINDOWS;C:\WINDOWS\system32\WBem
set PYTHONPATH=%OSGEO4W_ROOT%\apps\qgis-ltr\python;%OSGEO4W_ROOT%\apps\Python312\Lib\site-packages

echo ========================================
echo QGIS-Agent v2.0.0 Portable
echo ========================================

"%OSGEO4W_ROOT%\apps\Python312\python.exe" "%~dp0src\main.py"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   Start failed, exit code: %ERRORLEVEL%
    pause
)