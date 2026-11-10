; Instalador da Mesa para Windows (NSIS 3). Gerado por build-windows.sh, que monta a pasta "pacote":
;   MesaFX.dll, mesa_retorno.exe, mesa.py, mesa_win.py, ui.html, mesa.ico, python\ (Python embutido oficial)
; O que ele faz além de copiar:
;   - %ProgramData%\Mesa: o usuário grava a cadeia, o serviço de áudio (LOCAL SERVICE) lê
;   - <config do Equalizer APO>\mesa.txt, que o usuário pode gravar, e "Include: mesa.txt" no config.txt
;   - Equalizer APO embutido (pasta "eapo", tirada do instalador oficial): se ele não estiver instalado,
;     instala igual ao instalador dele, sem o Device Selector; mesa_apo.py liga o microfone e a saída
;     padrão e testa (o que o Device Selector faria). Na desinstalação, só remove se foi a Mesa que instalou.
Unicode true
!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"

!ifndef VERSAO
  !define VERSAO "2.0"
!endif
Name "Mesa"
OutFile "..\dist\Mesa-${VERSAO}-Setup.exe"
InstallDir "$PROGRAMFILES64\Mesa"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
BrandingText "Mesa ${VERSAO}"

!define MUI_ICON "pacote\mesa.ico"
!define MUI_UNICON "pacote\mesa.ico"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Abrir a Mesa"
!define MUI_FINISHPAGE_RUN_FUNCTION AbrirMesa
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "PortugueseBR"

Var EAPO
Var EAPODIR

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "A Mesa precisa do Windows de 64 bits."
    Abort
  ${EndIf}
  SetRegView 64
FunctionEnd

Function un.onInit
  SetRegView 64
FunctionEnd

; abre como o usuário comum (o instalador roda como administrador)
Function AbrirMesa
  Exec '"$WINDIR\explorer.exe" "$SMPROGRAMS\Mesa.lnk"'
FunctionEnd

Section "Mesa"
  SetShellVarContext all
  SetOutPath "$INSTDIR"
  ; atualização: o serviço de áudio pode estar com o plugin aberto; um DLL carregado não pode ser
  ; sobrescrito, mas pode ser renomeado (o antigo some no próximo boot)
  ${If} ${FileExists} "$INSTDIR\MesaFX.dll"
    Delete "$INSTDIR\MesaFX.antigo.dll"
    Rename "$INSTDIR\MesaFX.dll" "$INSTDIR\MesaFX.antigo.dll"
    Delete /REBOOTOK "$INSTDIR\MesaFX.antigo.dll"
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\mesa_retorno.exe"   ; o retorno pode estar ligado
    Delete "$INSTDIR\mesa_retorno.antigo.exe"
    Rename "$INSTDIR\mesa_retorno.exe" "$INSTDIR\mesa_retorno.antigo.exe"
    Delete /REBOOTOK "$INSTDIR\mesa_retorno.antigo.exe"
  ${EndIf}
  File /r "pacote\*.*"

  ; cadeia salva: o usuário escreve, o serviço de áudio lê
  CreateDirectory "$APPDATA\Mesa"
  nsExec::Exec '"$SYSDIR\icacls.exe" "$APPDATA\Mesa" /grant *S-1-5-32-545:(OI)(CI)M *S-1-5-19:(OI)(CI)RX'
  Pop $0

  ReadRegStr $EAPO HKLM "SOFTWARE\EqualizerAPO" "ConfigPath"
  ReadRegStr $EAPODIR HKLM "SOFTWARE\EqualizerAPO" "InstallPath"
  ; o desinstalador do Equalizer APO deixa a pasta config e a chave do registro: conta só se o DLL existir
  ${If} $EAPO == ""
  ${OrIfNot} ${FileExists} "$EAPO\config.txt"
  ${OrIfNot} ${FileExists} "$EAPODIR\EqualizerAPO.dll"
    ; Equalizer APO embutido: o mesmo que a seção de instalação dele faz (Setup.nsi 1.4.2)
    DetailPrint "Instalando o Equalizer APO 1.4.2"
    StrCpy $EAPODIR "$PROGRAMFILES64\EqualizerAPO"
    SetOutPath "$EAPODIR"
    File /r "eapo\*.*"
    CreateDirectory "$EAPODIR\config"
    CreateDirectory "$EAPODIR\VSTPlugins"
    FileOpen $0 "$EAPODIR\config\config.txt" w
    FileWrite $0 "Include: mesa.txt$\r$\n"
    FileClose $0
    FileOpen $0 "$EAPODIR\config\mesa.txt" w
    FileWrite $0 "# Gerado pela Mesa$\r$\n"
    FileClose $0
    nsExec::Exec '"$SYSDIR\icacls.exe" "$EAPODIR\config" /grant *S-1-5-32-545:(OI)(CI)F'
    Pop $0
    WriteRegStr HKLM "SOFTWARE\EqualizerAPO" "InstallPath" "$EAPODIR"
    WriteRegStr HKLM "SOFTWARE\EqualizerAPO" "ConfigPath" "$EAPODIR\config"
    WriteRegStr HKLM "SOFTWARE\EqualizerAPO" "EnableTrace" "false"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Audio" "DisableProtectedAudioDG" 1
    ExecWait '"$SYSDIR\regsvr32.exe" /s "$EAPODIR\EqualizerAPO.dll"'
    WriteRegDWORD HKLM "Software\Mesa" "InstalouEqualizerAPO" 1
    CreateDirectory "$SMPROGRAMS\Equalizer APO (Mesa)"
    CreateShortCut "$SMPROGRAMS\Equalizer APO (Mesa)\Equalizer APO Configuration Editor.lnk" "$EAPODIR\Editor.exe"
    CreateShortCut "$SMPROGRAMS\Equalizer APO (Mesa)\Equalizer APO Device Selector.lnk" "$EAPODIR\DeviceSelector.exe"
    SetOutPath "$INSTDIR"
  ${Else}
    ${IfNot} ${FileExists} "$EAPO\mesa.txt"
      FileOpen $0 "$EAPO\mesa.txt" w
      FileWrite $0 "# Gerado pela Mesa$\r$\n"
      FileClose $0
    ${EndIf}
    nsExec::Exec '"$SYSDIR\icacls.exe" "$EAPO\mesa.txt" /grant *S-1-5-32-545:M'
    Pop $0
    ; "Include: mesa.txt" no fim do config.txt (uma vez só)
    StrCpy $2 0
    ClearErrors
    FileOpen $0 "$EAPO\config.txt" r
    ${Do}
      FileRead $0 $1
      ${If} ${Errors}
        ${ExitDo}
      ${EndIf}
      StrCpy $1 $1 17
      ${If} $1 == "Include: mesa.txt"
        StrCpy $2 1
      ${EndIf}
    ${Loop}
    FileClose $0
    ${If} $2 == 0
      FileOpen $0 "$EAPO\config.txt" a
      FileSeek $0 0 END
      FileWrite $0 "$\r$\nInclude: mesa.txt$\r$\n"
      FileClose $0
    ${EndIf}
  ${EndIf}

  ; liga o Equalizer APO no microfone e na saída padrão do Windows e testa (reinicia o serviço de áudio)
  DetailPrint "Ligando o Equalizer APO no microfone e na saída padrão"
  nsExec::ExecToLog '"$INSTDIR\python\python.exe" "$INSTDIR\mesa_apo.py" instalar --entrada padrao --saida padrao'
  Pop $0

  CreateShortCut "$SMPROGRAMS\Mesa.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\mesa_win.py"' "$INSTDIR\mesa.ico"
  CreateShortCut "$DESKTOP\Mesa.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\mesa_win.py"' "$INSTDIR\mesa.ico"

  WriteUninstaller "$INSTDIR\Desinstalar.exe"
  !define UNINST "Software\Microsoft\Windows\CurrentVersion\Uninstall\Mesa"
  WriteRegStr HKLM "${UNINST}" "DisplayName" "Mesa"
  WriteRegStr HKLM "${UNINST}" "DisplayVersion" "${VERSAO}"
  WriteRegStr HKLM "${UNINST}" "Publisher" "Mesa"
  WriteRegStr HKLM "${UNINST}" "DisplayIcon" "$INSTDIR\mesa.ico"
  WriteRegStr HKLM "${UNINST}" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
  WriteRegDWORD HKLM "${UNINST}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext all
  ReadRegStr $EAPO HKLM "SOFTWARE\EqualizerAPO" "ConfigPath"
  ${If} $EAPO != ""
  ${AndIf} ${FileExists} "$EAPO\config.txt"
    ; tira o Include do config.txt (copia sem a linha) e apaga o mesa.txt: o Equalizer APO recarrega sem a Mesa
    ClearErrors
    FileOpen $0 "$EAPO\config.txt" r
    FileOpen $3 "$EAPO\config.txt.mesa" w
    ${Do}
      FileRead $0 $1
      ${If} ${Errors}
        ${ExitDo}
      ${EndIf}
      StrCpy $2 $1 17
      ${If} $2 != "Include: mesa.txt"
        FileWrite $3 $1
      ${EndIf}
    ${Loop}
    FileClose $0
    FileClose $3
    Delete "$EAPO\config.txt"
    Rename "$EAPO\config.txt.mesa" "$EAPO\config.txt"
    Delete "$EAPO\mesa.txt"
  ${EndIf}
  ; Equalizer APO: só sai se foi a Mesa que instalou (o Device Selector /u devolve os APOs originais)
  ReadRegDWORD $0 HKLM "Software\Mesa" "InstalouEqualizerAPO"
  ReadRegStr $EAPODIR HKLM "SOFTWARE\EqualizerAPO" "InstallPath"
  ${If} $0 == 1
  ${AndIf} $EAPODIR != ""
    SetOutPath "$EAPODIR"
    ExecWait '"$EAPODIR\DeviceSelector.exe" /u'
    ExecWait '"$SYSDIR\regsvr32.exe" /u /s "$EAPODIR\EqualizerAPO.dll"'
    SetOutPath "$TEMP"
    nsExec::Exec 'powershell -NoProfile -Command "Restart-Service Audiosrv -Force"'
    Pop $0
    RMDir /r /REBOOTOK "$EAPODIR"
    DeleteRegKey HKLM "SOFTWARE\EqualizerAPO"
    RMDir /r "$SMPROGRAMS\Equalizer APO (Mesa)"
  ${EndIf}
  DeleteRegKey HKLM "Software\Mesa"
  Delete "$SMPROGRAMS\Mesa.lnk"
  Delete "$DESKTOP\Mesa.lnk"
  RMDir /r /REBOOTOK "$INSTDIR"
  RMDir /r "$APPDATA\Mesa"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Mesa"
SectionEnd
