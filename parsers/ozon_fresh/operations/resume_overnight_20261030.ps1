$ErrorActionPreference = 'Stop'
$taskWork = 'C:\Users\maxik\Documents\Codex\2026-10-17\ghbd\work'
$taskProject = 'C:\Users\maxik\PycharmProjects\PythonProject\fresh_fast'
$taskPython = 'C:\Users\maxik\PycharmProjects\PythonProject\.venv\Scripts\python.exe'
$taskOut = Join-Path $taskProject 'results\full_shared_20261018_111443'
$taskAuth = Get-Content -LiteralPath (Join-Path $taskWork 'overnight_authorization_20261030.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $taskAuth.active) { throw 'Night collection authorization is inactive' }
if ($taskAuth.task_scope -eq 'metadata_backfill_only') { throw 'Current authorization is metadata-only; full catalog crawl remains stopped' }
$taskLock = [IO.File]::Open((Join-Path $taskWork 'overnight_resume.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $taskMetaPath = Join-Path $taskWork 'active_managed_launch.json'
    $taskOld = Get-Content -LiteralPath $taskMetaPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskActive = Get-Content -LiteralPath (Join-Path $taskProject 'results\active_shared_run.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskOld.out_dir -ne $taskOut -or $taskActive.out_dir -ne $taskOut -or $taskActive.started_at -ne '2026-10-18 11:14:43') { throw 'Unexpected snapshot; do not start a different collection' }
    if ($taskOld.user_requested_stop -and $taskOld.user_requested_stop_unix -gt $taskAuth.authorized_unix) { throw 'A newer user stop overrides this authorization' }
    $taskStatus = Get-Content -LiteralPath (Join-Path $taskOut 'run_status.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskBackoff = Get-Content -LiteralPath (Join-Path $taskOut 'backoff_status.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskGuard = Get-Content -LiteralPath (Join-Path $taskProject 'work\browser_transport_9222.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskActive.complete -or $taskActive.processed_all_zones -or $taskStatus.complete -or $taskStatus.status -in @('complete','finished_with_gaps','finished')) { Write-Output 'Collection already finished'; exit 0 }
    $taskExisting = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'run_parser.py|ozon_live_parser.py' })
    if ($taskExisting.Count) { Write-Output 'A collector already exists; no duplicate started'; exit 0 }
    if ($taskStatus.requires_manual_access_check -or $taskBackoff.requires_manual_access_check -or $taskBackoff.manual_required_since_unix -gt 0 -or ($taskGuard.blocked -and $taskGuard.reason -match 'captcha|капч|challenge|manual access|authenticated')) {
        Write-Output 'Waiting for a fresh manual access confirmation; no collector started'; exit 0
    }
    $taskSocket = [Net.Sockets.TcpClient]::new()
    try { $taskPort = $taskSocket.ConnectAsync('127.0.0.1',9222).Wait(2000) -and $taskSocket.Connected } catch { $taskPort = $false } finally { $taskSocket.Dispose() }
    if (-not $taskPort) { Write-Output 'Waiting for the existing parser Chrome on port 9222; browser launch is blocked by policy'; exit 0 }
    $taskTests = Get-Content -LiteralPath (Join-Path $taskWork 'all_test_results.txt') -Raw -Encoding UTF8
    if ($taskTests -notmatch 'Ran 418 tests' -or -not $taskTests.TrimEnd().EndsWith('OK')) { throw 'Expected successful repair tests' }
    $taskAudit = Get-Content -LiteralPath (Join-Path $taskWork 'managed_repair_20261030_fixed_http10\installation_audit.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskAudit.quick_check -ne 'ok' -or -not (Test-Path -LiteralPath $taskAudit.backup)) { throw 'Verified SQLite backup missing' }
    foreach ($taskEntry in $taskAudit.installed_files.PSObject.Properties) {
        if ((Get-FileHash -LiteralPath (Join-Path $taskProject $taskEntry.Name) -Algorithm SHA256).Hash -ne $taskEntry.Value) { throw "Repair source changed: $($taskEntry.Name); verify tests before starting" }
    }
    $taskStarted = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $taskLogs = Join-Path $taskWork ('managed_resume_' + $taskStarted)
    New-Item -ItemType Directory -Path $taskLogs -ErrorAction Stop | Out-Null
    Copy-Item -LiteralPath $taskMetaPath -Destination (Join-Path $taskLogs 'parent_metadata.json')
    Copy-Item -LiteralPath (Join-Path $taskOut 'run_status.json') -Destination (Join-Path $taskLogs 'previous_run_status.json')
    Copy-Item -LiteralPath (Join-Path $taskOut 'backoff_status.json') -Destination (Join-Path $taskLogs 'previous_backoff_status.json')
    $taskArgs = @('-u','-X','utf8',(Join-Path $taskProject 'run_parser.py'),'--action','full','--resume-current','--price-workers','7','--catalog-workers','7','--interval','5','--max-interval','10','--http-error-steps','6,7,8,9,10','--http-error-statuses','403,429','--price-policy','catalog_metadata_cache','--continue-until-finished','--wait-on-403','--quarantine-incomplete')
    $taskMeta = [ordered]@{
        started_unix=$taskStarted; parent_started_unix=$taskOld.started_unix; root_started_unix=1793125197
        out_dir=$taskOut; stdout=(Join-Path $taskLogs 'stdout.log'); stderr=(Join-Path $taskLogs 'stderr.log')
        workers=7; interval=5; max_interval=10; on_403_interval=$null; on_429_interval=$null
        http_error_steps=@(6,7,8,9,10); decrease_after_error=$false; chrome_proxy=$null; no_proxy_server=$true
        resumed_at_user_request=$true; user_requested_stop=$false
        resume_reason='User requested overnight collection, 5-second base, +1 per HTTP403/429 episode to10, autonomous repair and restart'
        previous_stop_reason=$taskStatus.stop_reason; parser_repaired_for_previous_stop=$false
        repair_policy=(Join-Path $taskWork 'overnight_policy_20261030.md')
        offline_tests_passed=418; parser_source_changed=$true; live_progress_confirmed=$false
        monitor_type='codex_heartbeat'; authorization=(Join-Path $taskWork 'overnight_authorization_20261030.json')
    }
    if ($taskAuth.latest_verified_product_sku) { $taskArgs += @('--preferred-sku',[string]$taskAuth.latest_verified_product_sku) }
    $taskArgs += @('--metadata-store',(Join-Path $taskWork 'product_metadata_backfill.sqlite'))
    $taskCollector = Start-Process -FilePath $taskPython -ArgumentList $taskArgs -WorkingDirectory $taskProject -RedirectStandardOutput $taskMeta.stdout -RedirectStandardError $taskMeta.stderr -WindowStyle Hidden -PassThru
    $taskMeta.launcher_pid=$taskCollector.Id
    $taskMeta | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $taskMetaPath -Encoding UTF8
    [ordered]@{event='overnight_fixed_http5_resume';recorded_unix=$taskStarted;started_unix=$taskStarted;parent_started_unix=$taskOld.started_unix;out_dir=$taskOut;launcher_pid=$taskCollector.Id;on_403_interval_seconds=5;on_429_interval_seconds=5;server_retry_after_preserved=$true;manual_guard_preserved=$true;offline_tests_passed=418} | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $taskWork 'managed_repair_history.jsonl') -Encoding UTF8
    $taskMeta | ConvertTo-Json -Compress -Depth 6
} finally { $taskLock.Dispose() }
