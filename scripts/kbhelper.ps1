# KB Helper launcher for Windows (PowerShell 5.1+). Called by kbhelper.cmd in the project root.
#   kbhelper            start everything and open the browser (same as: kbhelper start)
#   kbhelper stop       stop the API server (database and Ollama keep running)
#   kbhelper restart    stop + start
#   kbhelper status     what is running
#   kbhelper logs       follow the server log
#   kbhelper install    add the project folder to your PATH, so `kbhelper` works from any folder
param([string]$Command = "start")

$ErrorActionPreference = "Continue"   # PS 5.1 turns native stderr into terminating errors under "Stop"
$ProgressPreference = "SilentlyContinue"

$Dir        = Split-Path -Parent $PSScriptRoot
Set-Location $Dir
$Port       = if ($env:KB_PORT) { $env:KB_PORT } else { "8091" }
$Url        = "http://localhost:$Port"
$Ollama     = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST.TrimEnd("/") } else { "http://localhost:11434" }
$ChatModel  = if ($env:KB_CHAT_MODEL) { $env:KB_CHAT_MODEL } else { "qwen3:1.7b" }
$EmbedModel = if ($env:KB_EMBED_MODEL) { $env:KB_EMBED_MODEL } else { "nomic-embed-text" }
$PidFile    = Join-Path $Dir ".kb.pid"
$Log        = Join-Path $Dir "api.log"
$OutLog     = Join-Path $Dir "api.out.log"
$Db         = "kb_vectordb"
$VenvPy     = Join-Path $Dir ".venv\Scripts\python.exe"

function Step($t) { Write-Host "> " -ForegroundColor Red -NoNewline; Write-Host $t -NoNewline }
function Ok($t = "") { Write-Host " OK " -ForegroundColor Green -NoNewline; Write-Host $t }
function Die($t) { Write-Host ""; Write-Host "X $t" -ForegroundColor Red; exit 1 }
function Banner {
  Write-Host ""
  Write-Host "  KB HELPER" -ForegroundColor Red -NoNewline
  Write-Host "  - Allied Testing knowledge assistant" -ForegroundColor DarkGray
  Write-Host ""
}

function Test-Url($u) {
  try { Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}
function Test-Docker { if (-not (Has "docker")) { return $false }; & docker info *> $null; return ($LASTEXITCODE -eq 0) }
function Test-Server { return (Test-Url "$Url/kb/stats") }
function Has($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Wait-For <seconds> { condition }: retry until the script block returns $true
function Wait-For([int]$Seconds, [scriptblock]$Check) {
  for ($i = 0; $i -lt $Seconds; $i++) {
    if (& $Check) { return $true }
    Start-Sleep -Seconds 1; Write-Host "." -NoNewline
  }
  return $false
}

function Find-Python {
  if (Has "py") { & py -3 --version *> $null; if ($LASTEXITCODE -eq 0) { return @("py", "-3") } }
  if (Has "python") { & python --version *> $null; if ($LASTEXITCODE -eq 0) { return @("python") } }
  return $null
}

function Sql($q) {
  $r = & docker exec $Db psql -U kbuser -d knowledge_db -tAc $q 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $r) { return "0" }
  return ("$r").Trim()
}

function Start-Kb {
  Banner
  if (Test-Server) { Step "Already running"; Ok $Url; Start-Process $Url; return }

  # 1. Docker
  Step "Docker"
  if (-not (Has "docker")) { Die "Docker is not installed: https://www.docker.com/products/docker-desktop/" }
  if (-not (Test-Docker)) {
    $exe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $exe) { Start-Process $exe }
    if (-not (Wait-For 120 { Test-Docker })) { Die "Docker did not start. Open Docker Desktop and try again." }
  }
  Ok

  # 2. database: on Windows the data lives in a Docker volume (Postgres cannot use an NTFS bind mount)
  Step "Database ($Db)"
  & docker container inspect $Db *> $null
  if ($LASTEXITCODE -eq 0) {
    & docker start $Db *> $null
  } else {
    & docker compose -f docker-compose.yaml -f docker-compose.windows.yaml up -d $Db *> $null
    if ($LASTEXITCODE -ne 0) { Die "could not create $Db" }
  }
  if (-not (Wait-For 60 { & docker exec $Db pg_isready -U kbuser -d knowledge_db *> $null; $LASTEXITCODE -eq 0 })) { Die "database is not ready" }
  Ok "port 5435"

  # 3. Ollama + models
  Step "Ollama"
  if (-not (Test-Url "$Ollama/api/tags")) {
    if (-not (Has "ollama")) { Die "Ollama is not installed: https://ollama.com/download" }
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
    if (-not (Wait-For 60 { Test-Url "$Ollama/api/tags" })) { Die "Ollama did not start." }
  }
  $tags = (Invoke-RestMethod "$Ollama/api/tags").models.name
  foreach ($m in @($EmbedModel, $ChatModel)) {
    if (-not ($tags | Where-Object { $_ -like "$m*" })) {
      Write-Host ""; Write-Host "  pulling $m..."
      & ollama pull $m
      if ($LASTEXITCODE -ne 0) { Die "could not pull $m" }
    }
  }
  Ok "$ChatModel - $EmbedModel"

  # 4. Python environment (first run only)
  Step "Python env"
  if (-not (Test-Path (Join-Path $Dir ".venv\Scripts\uvicorn.exe"))) {
    $py = Find-Python
    if (-not $py) { Die "Python 3 is not installed: https://www.python.org/downloads/" }
    Write-Host ""; Write-Host "  creating .venv and installing requirements..."
    $exe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1) + @("-m", "venv", ".venv")
    & $exe @pyArgs
    & $VenvPy -m pip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
  }
  Ok

  # 5. knowledge base (first run only: load + embed when the table is empty)
  Step "Knowledge base"
  $env:PYTHONUTF8 = "1"
  $chunks = Sql "SELECT count(*) FROM kb_chunks"
  if ($chunks -eq "0") {
    Write-Host ""; Write-Host "  empty, loading knowledge\ ..."
    & $VenvPy loader.py | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "loader.py failed" }
    $chunks = Sql "SELECT count(*) FROM kb_chunks"
  }
  $missing = Sql "SELECT count(*) FROM kb_chunks WHERE body_vec IS NULL"
  if ($missing -ne "0") {
    Write-Host ""; Write-Host "  embedding $missing chunks..."
    & $VenvPy vectorize.py | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "vectorize.py failed" }
  }
  Ok "$chunks chunks"

  # 6. API + web app, in the background
  Step "Server"
  if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Die "port $Port is busy with another program"
  }
  $p = Start-Process -FilePath $VenvPy -ArgumentList "-m", "uvicorn", "ragserver:app", "--port", $Port `
         -WorkingDirectory $Dir -WindowStyle Hidden -PassThru `
         -RedirectStandardOutput $OutLog -RedirectStandardError $Log
  Set-Content -Path $PidFile -Value $p.Id
  if (-not (Wait-For 60 { Test-Server })) { Die "server did not start, see: kbhelper logs" }
  Ok $Url

  # 7. warm the chat model so the first answer is fast
  Step "Warming up $ChatModel"
  try {
    Invoke-RestMethod -Method Post -Uri "$Ollama/api/generate" -TimeoutSec 120 `
      -Body (@{ model = $ChatModel; keep_alive = "30m" } | ConvertTo-Json) -ContentType "application/json" | Out-Null
  } catch { }
  Ok

  Write-Host ""
  Write-Host "  Ready -> " -NoNewline; Write-Host $Url -ForegroundColor Red
  Write-Host "  API docs: $Url/docs - stop: kbhelper stop - logs: kbhelper logs" -ForegroundColor DarkGray
  Write-Host ""
  Start-Process $Url
}

function Stop-Kb {
  Step "Stopping server"
  $stopped = $false
  if (Test-Path $PidFile) {
    $id = [int](Get-Content $PidFile)
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) { Stop-Process -Id $id -Force; $stopped = $true }
    Remove-Item $PidFile -Force
  }
  if (-not $stopped) {
    # started by hand: stop whatever listens on the port
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
      ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
  }
  Wait-For 10 { -not (Test-Server) } | Out-Null
  Ok
}

function Show-Status {
  Banner
  function Line($name, $up, $extra = "") {
    Write-Host ("  {0,-10} " -f $name) -NoNewline
    if ($up) { Write-Host "running" -ForegroundColor Green -NoNewline } else { Write-Host "stopped" -ForegroundColor Red -NoNewline }
    Write-Host "  $extra"
  }
  $docker = Test-Docker
  $dbUp = $false
  if ($docker) { $dbUp = ((& docker inspect -f "{{.State.Running}}" $Db 2>$null) -eq "true") }
  Line "Docker" $docker
  Line "Database" $dbUp
  Line "Ollama" (Test-Url "$Ollama/api/tags")
  $srv = Test-Server
  Line "Server" $srv $(if ($srv) { $Url } else { "" })
  if ($srv) { Write-Host ("  " + (Invoke-WebRequest "$Url/kb/stats" -UseBasicParsing).Content) -ForegroundColor DarkGray }
  Write-Host ""
}

function Install-Kb {
  Step "Adding $Dir to your PATH"
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $parts = @($userPath -split ";" | Where-Object { $_ -and ($_.TrimEnd("\") -ne $Dir.TrimEnd("\")) })
  [Environment]::SetEnvironmentVariable("Path", (($parts + $Dir) -join ";"), "User")
  Ok
  Write-Host ""
  Write-Host "  Open a new terminal, then just type " -NoNewline; Write-Host "kbhelper" -ForegroundColor Red
  Write-Host ""
}

switch ($Command.ToLower()) {
  "start"   { Start-Kb }
  "stop"    { Stop-Kb }
  "restart" { Stop-Kb; Start-Kb }
  "status"  { Show-Status }
  "logs"    { Get-Content $Log -Tail 50 -Wait }
  "install" { Install-Kb }
  default   { Write-Host "usage: kbhelper [start|stop|restart|status|logs|install]"; exit 1 }
}
