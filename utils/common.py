import re


def redact_sensitive(text: str) -> str:
    """
    脱敏敏感信息（手机号、身份证、密码、群聊名称等）
    """
    value = str(text or "")
    value = re.sub(r"\b1\d{10}\b", "[手机号已脱敏]", value)
    value = re.sub(r"\b\d{15,20}\b", "[编号已脱敏]", value)
    value = re.sub(r"(密码|pass(?:word)?)[：:\s]*[^\s，。;；\n\r]{1,64}", r"\1：[已脱敏]", value, flags=re.IGNORECASE)
    value = re.sub(r"(账号|account)[：:\s]*[^\s，。;；\n\r]{1,64}", r"\1：[已脱敏]", value, flags=re.IGNORECASE)
    return value
