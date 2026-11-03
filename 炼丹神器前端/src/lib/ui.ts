import { ElMessage, ElMessageBox } from "element-plus";
import { errorText, isCancelled } from "../api/client";
export function notifyError(error: unknown) {
  if (!isCancelled(error))
    ElMessage.error({
      message: errorText(error),
      duration: 6500,
      showClose: true,
    });
}
export async function confirmAction(text: string, title = "请确认操作") {
  try {
    await ElMessageBox.confirm(text, title, {
      confirmButtonText: "确认",
      cancelButtonText: "取消",
      type: "warning",
      distinguishCancelAndClose: true,
    });
    return true;
  } catch {
    return false;
  }
}
