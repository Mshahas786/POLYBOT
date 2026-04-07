$key = "C:\Users\Muhammed\Downloads\ssh-key-2026.key"
$ip = "152.67.120.35"
$user = "opc"
$sshOpts = "-o StrictHostKeyChecking=no -o ConnectTimeout=10"

Write-Host "Creating zip of files to upload..."
if (Test-Path "vps\deploy.zip") { Remove-Item "vps\deploy.zip" -Force }
Compress-Archive -Path "vps\api.py", "vps\bot.py", "vps\setup.sh" -DestinationPath "vps\deploy.zip" -Force

Write-Host "Uploading zip file to VPS..."
scp -i $key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "vps\deploy.zip" "${user}@${ip}:~/deploy.zip"

Write-Host "Executing deployment on VPS..."
$cmd = "sudo dnf install -y unzip && unzip -o ~/deploy.zip -d ~/polybot/ && cd ~/polybot && chmod +x setup.sh && ./setup.sh"
ssh -i $key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${user}@${ip}" $cmd

Write-Host "Deployment script finished."
