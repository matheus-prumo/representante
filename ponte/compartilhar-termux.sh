#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  Liga o "compartilhar com o Termux" para os prints do grupo.
#  WhatsApp > print > compartilhar > Termux  ->  app Representante
#  Rodar uma vez só.
# ============================================================
mkdir -p ~/bin ~/rep-app-dados/recebidos
ALVO=~/bin/termux-file-editor
if [ -f "$ALVO" ] && ! grep -q "rep-app-recebidos" "$ALVO"; then
  mv "$ALVO" "$ALVO.antes-do-representante"
  echo "Já existia um termux-file-editor seu. Guardei como termux-file-editor.antes-do-representante"
  echo "(ele continua sendo usado para arquivos que não são imagem)."
fi
cat > "$ALVO" <<'REP_FIM'
#!/data/data/com.termux/files/usr/bin/bash
# rep-app-recebidos: recebe prints compartilhados e manda para o app Representante
F="$1"
EXT="${F##*.}"; EXT="${EXT,,}"
case "$EXT" in
  jpg|jpeg|png|webp) ;;
  *)
    if [ -x ~/bin/termux-file-editor.antes-do-representante ]; then
      exec ~/bin/termux-file-editor.antes-do-representante "$F"
    fi
    echo "Este arquivo não é uma imagem: $F"
    exit 0 ;;
esac
mkdir -p ~/rep-app-dados/recebidos
NOME="$(date +%Y%m%d-%H%M%S)-$RANDOM.$EXT"
cp "$F" ~/rep-app-dados/recebidos/"$NOME" && rm -f "$F"
bash ~/rep-app/iniciar.sh --sem-abrir > /dev/null 2>&1
echo "Print enviado para o app Representante."
termux-open-url "https://matheus-prumo.github.io/representante/?termux=1"
REP_FIM
chmod +x "$ALVO"
echo ""
echo "Pronto! Agora, no WhatsApp: abra o print > compartilhar > Termux."
echo "Se o Termux perguntar o que fazer com o arquivo, escolha EDIT (Editar)."
