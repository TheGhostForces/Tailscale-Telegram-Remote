"""Генерирует TOTP-секрет и QR-ссылку для Google Authenticator / Aegis / 2FAS."""
import pyotp

secret = pyotp.random_base32()
print("TOTP_SECRET=" + secret)
print(pyotp.TOTP(secret).provisioning_uri(name="rc-bot", issuer_name="RemoteControl"))
print("Добавьте секрет в приложение-аутентификатор (вручную или по otpauth:// ссылке).")
