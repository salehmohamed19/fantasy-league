import socket
import subprocess
import sys

def get_local_ip():
    """جلب الـ IP المحلي الصحيح للشبكة"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # محاولة الاتصال بـ IP وهمي لمعرفة الكارت النشط في الشبكة المحلية
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    local_ip = get_local_ip()
    port = "8000"
    url = f"http://{local_ip}:{port}/"

    print("\n" + "=" * 50)
    print(f"🚀 Django Server is starting...")
    print(f"🔗 Access Link: \033[92m{url}\033[0m")
    print("=" * 50 + "\n")

    try:
        # تشغيل سيرفر الدجانجو على جميع الشبكات
        subprocess.run([sys.executable, "manage.py", "runserver", f"0.0.0.0:{port}"])
    except KeyboardInterrupt:
        print("\n🛑 Server stopped.")