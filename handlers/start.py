import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from database.db_async import adb
from database import db
from config import ADMIN_CONTACT, TUTORIAL_LINK
from middleware.auth import is_admin


_welcome_messages = {}

def register_welcome_messages(user_id: int, message_ids: list[int]):
    _welcome_messages[user_id] = message_ids

async def delete_welcome_messages(bot, user_id: int, chat_id: int):
    msg_ids = _welcome_messages.pop(user_id, [])
    if msg_ids:
        async def safe_delete(msg_id):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        await asyncio.gather(*(safe_delete(msg_id) for msg_id in msg_ids))

async def transition_to_handler(bot, user_id: int, chat_id: int, text: str, reply_markup=None, update: Update = None):
    # 1. Hapus command user jika update dikirim agar chat bersih dan smooth
    if update and update.message:
        try:
            await update.message.delete()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Gagal menghapus pesan user: %s", e)

    # Ambil pesan welcome yang sedang aktif
    msg_ids = _welcome_messages.pop(user_id, [])
    if msg_ids:
        # Pesan pertama (msg_ids[0]) adalah pesan bot yang panjang (menu utama).
        # Kita EDIT pesan panjang ini langsung menjadi prompt baru agar tidak menumpuk!
        edit_msg_id = msg_ids[0]
        
        # Pesan-pesan lain di bawahnya kita hapus semuanya
        delete_ids = msg_ids[1:]
        if delete_ids:
            async def safe_delete(msg_id):
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
            await asyncio.gather(*(safe_delete(msg_id) for msg_id in delete_ids))

        try:
            # Edit pesan welcome panjang secara langsung (lewati ReplyKeyboardRemove karena Telegram
            # tidak mengizinkan pengubahan reply_markup menjadi ReplyKeyboardRemove secara edit)
            from telegram import ReplyKeyboardRemove
            actual_markup = reply_markup if not isinstance(reply_markup, ReplyKeyboardRemove) else None
            
            msg = await bot.edit_message_text(
                chat_id=chat_id,
                message_id=edit_msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=actual_markup,
                disable_web_page_preview=True
            )
            # Daftarkan kembali pesan yang diedit ini sebagai welcome message aktif
            register_welcome_messages(user_id, [edit_msg_id])
            return msg
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Gagal mengedit pesan welcome: %s", e)

    # Fallback: Kirim pesan baru jika edit gagal / tidak ada welcome messages
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )
    return msg


def get_start_keyboard() -> ReplyKeyboardMarkup:
    keyboard_buttons = [
        [KeyboardButton("/txttovcf"), KeyboardButton("/vcftotxt"), KeyboardButton("/xlsxtotxt"), KeyboardButton("/admin")],
        [KeyboardButton("/merge"),    KeyboardButton("/pecahvcf"), KeyboardButton("/rename"),    KeyboardButton("/duplikat")],
        [KeyboardButton("/count"),    KeyboardButton("/vip"),      KeyboardButton("/referal"),  KeyboardButton("/akun")],
        [KeyboardButton("/reset"),    KeyboardButton("/done"),     KeyboardButton("/start")],
    ]
    # Menggunakan one_time_keyboard=True agar keyboard bawah otomatis sembunyi begitu diklik!
    return ReplyKeyboardMarkup(keyboard_buttons, resize_keyboard=True, one_time_keyboard=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name or "Kawan"

    # ── Bangun menu ────────────────────────────────────────────────────────────
    fitur = (
        "<b>FITUR UTAMA</b>\n"
        "├ /txttovcf — Konversi TXT ke VCF\n"
        "├ /vcftotxt — Konversi VCF ke TXT\n"
        "├ /xlsxtotxt — Ekstrak Excel/CSV\n"
        "├ /admin — Buat file Admin VCF\n"
        "├ /merge — Gabungkan file VCF/TXT\n"
        "├ /pecahvcf — Pecah file VCF\n"
        "├ /rename — Ganti nama VCF\n"
        "├ /duplikat — Bersihkan duplikat\n"
        "└ /count — Hitung kontak\n\n"
        "<b>LAINNYA</b>\n"
        "├ /vip — Paket VIP\n"
        "├ /referal — VIP Gratis\n"
        "├ /akun — Info akun\n"
        "├ /reset — Bersihkan sesi\n"
        "└ /done — Selesaikan proses"
    )
    if is_admin(user.id):
        fitur += (
            "\n\n<b>ADMIN</b>\n"
            "├ /stat — Statistik\n"
            "├ /daftar — Daftar user\n"
            "├ /broadcast — Broadcast\n"
            "├ /addvip /delvip — Kelola VIP\n"
            "└ /resetdatabase — Bersihkan cache"
        )

    # ── Kirim msg1 (Welcome menu) dengan tombol inline hijau premium ──
    msg1 = await update.message.reply_text(
        text=(
            f"<b>Halo {first_name}!</b> Selamat datang di bot konversi kontak.\n\n"
            f"{fitur}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"<b>Owner:</b> {ADMIN_CONTACT}"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 TUTORIAL LENGKAP 🟢", url=TUTORIAL_LINK)]]),
        disable_web_page_preview=True,
    )

    # ── Kirim msg2 (Keyboard helper) pembawa custom keyboard bawah ──
    msg2 = await update.message.reply_text(
        text="<b>Silakan pilih menu di bawah:</b>",
        parse_mode="HTML",
        reply_markup=get_start_keyboard(),
    )

    register_welcome_messages(user.id, [msg1.message_id, msg2.message_id])


    # ── Semua DB + cleanup di background ──────────────────────────────────────
    async def _bg():
        try:
            is_new = (await adb.get_user(user.id)) is None
            await adb.upsert_user(user.id, user.username or "", user.full_name or "")
            await adb.increment_usage(user.id)

            if is_new:
                # Berikan VIP 30 hari gratis otomatis ke pengguna baru
                await adb.set_member_vip(user.id, 14, user.full_name or "New User")
                try:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=(
                            "🎁 <b>HADIAH PENGGUNA BARU!</b>\n\n"
                            "Selamat! Karena Anda baru pertama kali menggunakan bot ini, "
                            "Anda mendapatkan <b>Akses VIP 14 Hari GRATIS</b> secara otomatis!\n\n"
                            "Silakan nikmati semua fitur premium konversi kontak kami sepuasnya."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            if is_new and context.args:
                arg = context.args[0]
                if arg.startswith("ref_"):
                    try:
                        ref_id = int(arg.replace("ref_", ""))
                        if ref_id != user.id:
                            await adb.set_referrer(user.id, ref_id)
                            count = await adb.get_referral_count(ref_id)
                            if count > 0 and count % 5 == 0:
                                await adb.set_member_vip(ref_id, 7, "Referral Bonus")
                                await context.bot.send_message(
                                    chat_id=ref_id,
                                    text=f"Bonus referral! Teman ke-{count} bergabung. Kamu dapat 7 hari VIP gratis.",
                                )
                            else:
                                sisa = 5 - (count % 5)
                                await context.bot.send_message(
                                    chat_id=ref_id,
                                    text=f"Teman baru bergabung! Total {count} orang. Undang {sisa} lagi untuk VIP gratis.",
                                )
                    except ValueError:
                        pass

            db.clear_session(user.id)
            try:
                from middleware.session import clear_user_dir
                from handlers.cancel_helper import cancel_all
                cancel_all(user.id)
                clear_user_dir(user.id)
            except Exception:
                pass
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("[start] bg error: %s", exc)

    asyncio.create_task(_bg())


async def handle_back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler untuk kembali ke menu utama /start"""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    chat_id = query.message.chat_id
    first_name = user.first_name or "Kawan"

    # ── Bangun menu ────────────────────────────────────────────────────────────
    fitur = (
        "<b>FITUR UTAMA</b>\n"
        "├ /txttovcf — Konversi TXT ke VCF\n"
        "├ /vcftotxt — Konversi VCF ke TXT\n"
        "├ /xlsxtotxt — Ekstrak Excel/CSV\n"
        "├ /admin — Buat file Admin VCF\n"
        "├ /merge — Gabungkan file VCF/TXT\n"
        "├ /pecahvcf — Pecah file VCF\n"
        "├ /rename — Ganti nama VCF\n"
        "├ /duplikat — Bersihkan duplikat\n"
        "└ /count — Hitung kontak\n\n"
        "<b>LAINNYA</b>\n"
        "├ /vip — Paket VIP\n"
        "├ /referal — VIP Gratis\n"
        "├ /akun — Info akun\n"
        "├ /reset — Bersihkan sesi\n"
        "└ /done — Selesaikan proses"
    )
    if is_admin(user.id):
        fitur += (
            "\n\n<b>ADMIN</b>\n"
            "├ /stat — Statistik\n"
            "├ /daftar — Daftar user\n"
            "├ /broadcast — Broadcast\n"
            "├ /addvip /delvip — Kelola VIP\n"
            "└ /resetdatabase — Bersihkan cache"
        )

    menu_text = (
        f"<b>Halo {first_name}!</b> Selamat datang di bot konversi kontak.\n\n"
        f"{fitur}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"<b>Owner:</b> {ADMIN_CONTACT}"
    )

    # Selalu hapus pesan lama (tombol proses selesai) agar chat bersih
    try:
        await query.message.delete()
    except Exception:
        pass

    # Kirim ulang msg1 (Welcome menu) dengan tombol inline hijau premium
    msg1 = await context.bot.send_message(
        chat_id=chat_id,
        text=menu_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 TUTORIAL LENGKAP 🟢", url=TUTORIAL_LINK)]]),
        disable_web_page_preview=True,
    )

    # Kirim ulang msg2 (Keyboard helper) pembawa custom keyboard bawah
    msg2 = await context.bot.send_message(
        chat_id=chat_id,
        text="<b>Silakan pilih menu di bawah:</b>",
        parse_mode="HTML",
        reply_markup=get_start_keyboard(),
    )

    register_welcome_messages(user.id, [msg1.message_id, msg2.message_id])



