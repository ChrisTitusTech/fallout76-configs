# Run file by right-clicking and selecting "Run with PowerShell"

# Used to manually update HUDChallenges time stamps when SFE saving is not possible
# Can update Smiley trade time, for "showSmiley", and silo cooldown timers, for "showSiloCooldowns"

# HUDChallenges version 1.3.1


$dataFile = ".\ChallengeData.ini"

while(1)
{
	# Load file or create new json object from template
	$jsonObject = ""
	if([System.IO.File]::Exists($dataFile))
	{
		$jsonObject = Get-Content -Path $dataFile -Raw | ConvertFrom-JSON
		Write-Host File $dataFile loaded
		Write-Host
	}
	else
	{
		$jsonObject = ConvertFrom-Json "{'siloCooldowns': {},'smileyTrades': {}}"
		Write-Host File $dataFile not found, new file will be created if you add data
		Write-Host
	}


	# Choose data to update
	$updateProp = ""
	Write-Host "1 - Update silo cooldown time"
	Write-Host "2 - Update Smiley trade time"
	Write-Host "3 - EXIT"
	$updateChoice = Read-Host "Choice"
	Write-Host


	# Set property name to update based on user input
	if($updateChoice -eq 1)
	{
		Write-Host "Update silo cooldown for which character?"
		$updateProp = "siloCooldowns"
	}
	elseif($updateChoice -eq 2)
	{
		Write-Host "Update Smiley trade time for which character?"
		$updateProp = "smileyTrades"
	}
	elseif($updateChoice -eq 3)
	{
		return
	}

	if($updateProp -eq "")
	{
		Write-Host "Invalid choice!"
		Write-Host
		continue
	}


	# Check if selected property exists and create default value if not
	if(-not ([bool]($jsonObject.PSobject.Properties.name -match $updateProp)))
	{
		$dummyObj = [pscustomobject]@{}
		$jsonObject | Add-Member -MemberType NoteProperty -Name $updateProp -Value $dummyObj
	}
	

	# List character names to update selected property for
	$updateName = ""
	$names = @{}
	$namesCount = 1
	foreach($property in $jsonObject.$updateProp.PSobject.Properties.Name)
	{
		$names[[string]$namesCount] = $property
		Write-Host $namesCount - $property
		$namesCount++
	}
	Write-Host $namesCount - ADD NEW NAME
	$nameChoice = Read-Host "Choice"
	Write-Host


	# Get character name to update selected property for
	if(($nameChoice -gt $namesCount) -or ($nameChoice -lt 1))
	{
		Write-Host "Invalid choice!"
		continue
	}
	elseif($nameChoice -eq  $namesCount)
	{
		$updateName = Read-Host "New name"
		$dummyObj = [pscustomobject]@{}
		$jsonObject.$updateProp | Add-Member -MemberType NoteProperty -Name $updateName -Value $dummyObj
		Write-Host
	}
	else
	{
		$updateName = $names[$nameChoice]
	}

	if($updateName -eq "")
	{
		Write-Host "Error updating data, invalid character name!"
		Write-Host
		continue
	}


	# Choose silo if siloCooldowns property was selected
	$updateData = "time"
	if($updateChoice -eq 1)
	{
		$dataChoice = 0
		while($dataChoice -lt 1 -or $dataChoice -gt 4)
		{
			Write-Host "Update time for which silo?"
			Write-Host "1 - Alpha"
			Write-Host "2 - Bravo"
			Write-Host "3 - Charlie"
			Write-Host "4 - CANCEL"
			$dataChoice = Read-Host "Choice"
			Write-Host
		}
		if($dataChoice -eq 4)
		{
			continue
		}
		$updateData = $dataChoice - 1
	}


	# Update or add timestamp for selected property and character name
	$currentTime = [DateTimeOffset]::Now.ToUnixTimeSeconds()
	if([bool]($jsonObject.$updateProp.$updateName.PSobject.Properties.name -match $updateData))
	{
		$jsonObject.$updateProp.$updateName.$updateData = $currentTime
		Write-Host Updated $updateProp for char: $updateName
	}
	else
	{
		Write-Host Added $updateProp for char: $updateName
		$jsonObject.$updateProp.$updateName | Add-Member -MemberType NoteProperty -Name $updateData -Value $currentTime
		if($updateChoice -eq 2)
		{
			$jsonObject.$updateProp.$updateName | Add-Member -MemberType NoteProperty -Name "gold" -Value 300
		}
	}


	# Save changes to file
	$json = $jsonObject | ConvertTo-Json -Compress
	$json | Set-Content -Path $dataFile
	Write-Host File $dataFile saved
	Write-Host Changes should be visible in game in next 30s
	Write-Host
	Write-Host
	Write-Host
}
