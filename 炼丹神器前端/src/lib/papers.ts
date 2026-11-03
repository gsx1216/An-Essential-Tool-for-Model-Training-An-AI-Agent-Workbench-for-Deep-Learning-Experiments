const messages: Record<string, string> = {
  INVALID_PDF: "PDF 文件结构无法读取，请确认文件能正常打开后重新上传。",
  ENCRYPTED_PDF: "文件有密码保护，请先上传解除保护的副本。",
  NO_EXTRACTABLE_TEXT: "没有可提取的文字；扫描版 PDF 请先进行 OCR 识别。",
  PDF_LIMIT: "解析时超过资源限制，或解析进程异常结束，请尝试拆分文件。",
  PDF_TIMEOUT: "解析超时，可以重试或拆分文件。",
  PARSER_UNAVAILABLE: "解析器暂时不可用，请联系服务维护者检查系统日志。",
  PROCESS_INTERRUPTED: "上次解析已中断，请点击“重试解析”，无需重新上传。",
};
export function paperError(code: string | null) {
  return code
    ? messages[code] || `解析未完成（${code}），请重试或联系维护者。`
    : "";
}
