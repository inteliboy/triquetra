# Triquetra
Triquetra Updater for Windows

Software written in Python and compiled to an exe with Nuitka.

I am no programmer apart from ocasional batch scripting, however, in the age of AI anyone with an idea can create something.
Personally I always disabled Windows Update, as I prefer installing updates manually and hate Windows for updating drivers on its own. For some it is convenient but that is not I want.
I will try to add new features when I can, however, some tasks might be beyond me.

The new builds are automatically uploaded to the servers once a new UUP build is detected on the uupdump.net. It takes about 35 minutes per update for one architecture, so around 2 hours for Windows 11 23H2 amd64 + arm64 and Windows 11 25H2 amd64 + arch64 updates to be availabe in Triquetra.

Name and logo has been inspired by the German series Dark on Netflix.

Triquetra in action
![triquetra](https://github.com/user-attachments/assets/8342ee70-8709-44cd-88e1-264019b625e8)

Triquetra uses:
* An official SSU cab updates for Servicing Stack.
* An official NDP cab update for .NET Framework.
* An official EP cab update for Enablement Package.
* An ESD update made from CU update for the Cumulative Update.
Those updates come from UUP. I the use various scripts to extract WIM+PSF from the CU update, convert that to WIM and then to ESD to save space and bandwith.
* https://updates.smce.pl repository hosted with h5ai: login w11updater, password w11updater

Triquetra will:
* Create and operate in the C:\ProgramData\triquetra folder for updating itself, downloading updates and storing triquetra.log file.
* Check for administrative rights at startup.
* Check if the OS you are running it on is supported - only Client versions of Windows 11 22H2, 23H2, 24H2 and 25H2 are supported both for AMD64 and ARM64 - no support for Server and other editions.
* Check for Triquetra update on the basis of the exe hash and if available will update itself and re-launch.
* Determine a local build of Windows 11. It will say 26100.XXXX even for 25H2 (26200.XXXX), that is not a problem.
* Ask if you want to search for updates.
* Connect to update server or a mirror after determining which one is faster.
* Scan for possible update candidates and select newest by default. To force a specific build from the ones available a flag needs to be used: --build 26100.XXXX
* Ask if you want to download and install updates.
* Download and install Servicing Stack (SSU), Cumulative Update (CU) and .NET Framework (NDP) updates.
* Force installing build 26100.1742 - KB5043080 baseline if the Windows version is below that.
* Offer installing 23H2 / 25H2 Enablemenet Package (EP) when already running at least the minimum required Windows build - 22621.2506 / 26100.5074.
* Ask if you want to clean triquetra directory from the downloaded updates. If you choose no to do that on a subsequent run those updates will have their checksum verified and if they can be used, they will.
* Not download an incompletely uploaded build. There is a safety measure - a non_complete file that when placed in the build folder informs Triquetra that this update should be ignored for now as its beign uploaded to the server.
* Offer a reboot

Some AV software still gives false positives.- https://www.virustotal.com/gui/file/3403ac0c208805902a12c9a9e4a84e0848316bcd109fc63996c191885fed885b
<img width="1577" height="544" alt="obraz" src="https://github.com/user-attachments/assets/59821637-efd3-4abf-a8f9-e125cf13c82e" />




