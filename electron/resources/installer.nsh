; Scribe's additions to electron-builder's NSIS installer.
;
; 1. Uninstalling asks whether to remove the user's data - settings, API keys,
;    accounts and every recording in %APPDATA%\Scribe - with the box unticked.
;    A meeting archive is not something to delete because somebody removed an
;    app; a silent uninstall (and every upgrade) keeps it.
;
; 2. Before the app's files are removed, the uninstaller waits for
;    scribe-server.exe to finish. Closing Scribe to uninstall it stops a live
;    recording, and the server then writes that meeting up before exiting -
;    deleting the executable from under it would lose the notes, and would
;    fail anyway on a file that is still open.

!ifdef BUILD_UNINSTALLER

!include nsDialogs.nsh
!include LogicLib.nsh

Var ScribeRemoveData
Var ScribeRemoveBox

Function un.ScribeDataPage
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 36u "Your meetings, notes, settings and API keys are kept in $APPDATA\Scribe. They stay there after uninstalling unless you tick the box below."
  Pop $0
  ${NSD_CreateCheckbox} 0 46u 100% 12u "Also remove my data (recordings, notes, settings, accounts)"
  Pop $ScribeRemoveBox
  ${NSD_SetState} $ScribeRemoveBox ${BST_UNCHECKED}
  nsDialogs::Show
FunctionEnd

Function un.ScribeDataPageLeave
  ${NSD_GetState} $ScribeRemoveBox $ScribeRemoveData
FunctionEnd

!macro customUnWelcomePage
  !insertmacro MUI_UNPAGE_WELCOME
  UninstPage custom un.ScribeDataPage un.ScribeDataPageLeave
!macroend


!macro customUnInstall
  ; Runs once Scribe.exe has been closed and before any file is removed - on
  ; upgrades too, which replace scribe-server.exe the same way. Up to 150 s
  ; for the server to finish its write-up, checked once a second.
  StrCpy $R1 0
  ${Do}
    nsExec::ExecToStack 'cmd /c tasklist /NH /FI "IMAGENAME eq scribe-server.exe" | find /I "scribe-server.exe"'
    Pop $R0
    Pop $R2
    ${If} $R0 != 0
      ${Break}
    ${EndIf}
    IntOp $R1 $R1 + 1
    ${If} $R1 >= 150
      nsExec::Exec 'taskkill /F /T /IM scribe-server.exe'
      Pop $R0
      Sleep 1000
      ${Break}
    ${EndIf}
    Sleep 1000
  ${Loop}

  ${ifNot} ${isUpdated}
    ; Start at sign-in, as Electron registered it (named by the AppUserModelID).
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "com.neilsharma.scribe"
    ${If} $ScribeRemoveData == ${BST_CHECKED}
      RMDir /r "$APPDATA\Scribe"
    ${EndIf}
  ${endIf}
!macroend

!endif
