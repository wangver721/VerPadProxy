#!/data/data/com.termux/files/usr/bin/env bash
# 清理安卓端 /sdcard/VerPadProxy 下一次性调试脚本与旧日志。
# 保留：start_same_as_pc_root.sh / restore_default_hotspot.sh / _mitmdump.*、mitm_users.json、payload/data/scripts
set +e
echo "[1/3] 删除 /sdcard/VerPadProxy 下的临时脚本/日志"
for f in \
  /sdcard/VerPadProxy/_STATUS \
  /sdcard/VerPadProxy/_apdiag.sh \
  /sdcard/VerPadProxy/_check.log \
  /sdcard/VerPadProxy/_fix_crlf.sh \
  /sdcard/VerPadProxy/_net_diag.sh \
  /sdcard/VerPadProxy/_netdiag.sh \
  /sdcard/VerPadProxy/_proxy_diag.sh \
  /sdcard/VerPadProxy/_pymupdf_install.log \
  /sdcard/VerPadProxy/_pymupdf_ultimate.log \
  /sdcard/VerPadProxy/_quickdiag.sh \
  /sdcard/VerPadProxy/_root_apply_scope.sh \
  /sdcard/VerPadProxy/_root_restart_clean.sh \
  /sdcard/VerPadProxy/install_pymupdf.sh \
  /sdcard/VerPadProxy/install_pymupdf_all.sh \
  /sdcard/VerPadProxy/install_pymupdf_ultimate.sh \
  /sdcard/VerPadProxy/start_proxy_root.sh \
  /sdcard/VerPadProxy/scripts/termux/install_fixed_hotspot.sh; do
  if [ -e "$f" ]; then
    rm -f "$f" && echo "  removed $f"
  fi
done
rm -rf /sdcard/VerPadProxy/.stages && echo "  removed /sdcard/VerPadProxy/.stages" 2>/dev/null || true

echo "[2/3] 按尺寸裁剪大日志（保留最近 2000 行）"
for log in /sdcard/VerPadProxy/_mitmdump.log \
           /sdcard/VerPadProxy/scripts/mitm_visit.log \
           /sdcard/VerPadProxy/scripts/mitm_exit_telemetry.log; do
  if [ -f "$log" ]; then
    tail -n 2000 "$log" > "$log.tmp" && mv "$log.tmp" "$log" && echo "  trimmed $log"
  fi
done

echo "[3/3] 最终文件清单"
ls -la /sdcard/VerPadProxy/ 2>/dev/null
echo "---"
ls -la /sdcard/VerPadProxy/scripts/termux/ 2>/dev/null
