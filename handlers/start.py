import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from database.db_async import adb
from database import db
from config import ADMIN_CONTACT, TUTORIAL_LINK
from middleware.auth import is_admin


_welcome_messages = {}

def register_welcome_messages(user_id: int, message_ids: list[int]):
    _welcome_messages[user_id] = message_ids

def clear_welcome_messages(user_id: int):
    """Hapus tracking welcome message setelah proses selesai.
    Wajib dipanggil di akhir setiap handler agar command berikutnya
    tidak mengedit pesan lama yang sudah jauh ke atas."""
    _welcome_messages.pop(user_id, None)

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


def get_start_keyboard():
    """Stub: keyboard dihapus, semua handler tetap bisa import tanpa crash."""
    return None


def build_menu_text(first_name: str, user_id: int) -> str:
    fitur = (
        "<b>FITUR UTAMA</b>\n"
        "• /txttovcf — TXT KE VCF\n"
        "• /vcftotxt — VCF KE TXT\n"
        "• /admin — ADMIN NAVY VCF\n"
        "• /merge — GABUNG FILE\n"
        "• /count — HITUNG KONTAK\n"
        "• /xlsxtovcf — EXCEL KE VCF\n"
        "• /xlsxtotxt — EXCEL KE TXT\n"
        "• /pecahvcf — PECAH VCF\n"
        "• /pecahtxt — PECAH TXT\n"
        "• /rename — RENAME VCF\n"
        "• /addnum — TAMBAH KONTAK VCF\n"
        "• /duplikat — BERSIHKAN DUPLIKAT\n"
        "• /cleanup — CLEANUP NOMOR\n"
        "• /manual — KONTAK MANUAL\n"
        "• /walink — LINK WA EXCEL\n"
        "• /walinkweb — LINK WA HTML\n\n"
        "<b>LAINNYA</b>\n"
        "• /vip — PAKET VIP\n"
        "• /referal — VIP GRATIS\n"
        "• /akun — INFO AKUN\n"
        "• /reset — RESET SESI"
    )
    if is_admin(user_id):
        fitur += (
            "\n\n<b>ADMIN</b>\n"
            "• /stat — Statistik\n"
            "• /daftar — Daftar user\n"
            "• /backup — Backup database\n"
            "• /broadcast — Broadcast teks\n"
            "• /mediabroadcast — Broadcast media\n"
            "• /stopbroadcast — Hentikan broadcast\n"
            "• /addvip /delvip — Kelola VIP\n"
            "• /resetdatabase — Bersihkan cache"
        )

    header_text = "               <b>«  H A I F E E   C V  »</b>\n\n"
    return (
        f"{header_text}"
        f"Halo <b>{first_name}</b>! Selamat datang di <b>Haifee CV</b>.\n\n"
        f"{fitur}"
    )


async def send_fresh_start_menu(bot, user_id: int, chat_id: int, first_name: str):
    await delete_welcome_messages(bot, user_id, chat_id)
    menu_text = build_menu_text(first_name, user_id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("TUTORIAL", url=TUTORIAL_LINK, style="danger"),
            InlineKeyboardButton("DEVELOPER", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}", style="primary")
        ]
    ])

    msg1 = await bot.send_message(
        chat_id=chat_id,
        text=menu_text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    register_welcome_messages(user_id, [msg1.message_id])
    return msg1


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name or "Kawan"

    # Kirim fresh start menu secara bersih!
    await send_fresh_start_menu(context.bot, user.id, update.effective_chat.id, first_name)

    # ── Semua DB + cleanup di background ──────────────────────────────────────
    async def _bg():
        try:
            is_new = (await adb.get_user(user.id)) is None
            await adb.upsert_user(user.id, user.username or "", user.full_name or "")
            await adb.increment_usage(user.id)

            if is_new:
                # Berikan VIP 14 hari gratis otomatis ke pengguna baru
                await adb.set_member_vip(user.id, 14, user.full_name or "New User")
                try:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=(
                            "<b>Hadiah Pengguna Baru!</b>\n\n"
                            "Selamat! Kamu otomatis mendapatkan akses <b>VIP 14 Hari GRATIS</b>.\n"
                            "Nikmati semua fitur premium konversi kontak sepuasnya."
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
                            points_added = await adb.add_referral_points(ref_id, 1)
                            if points_added:
                                pts = await adb.get_referral_points(ref_id)
                                try:
                                    await context.bot.send_message(
                                        chat_id=ref_id,
                                        text=(
                                            "<b>Teman baru bergabung!</b>\n\n"
                                            "Kamu berhasil mengundang teman baru dan mendapatkan <b>1 Poin</b>.\n"
                                            f"• Saldo Poin: <b>{pts['referral_points']} Poin</b>\n"
                                            f"• Total Akumulasi: <b>{pts['total_referral_points_earned']}/50 Poin</b>\n\n"
                                            "Kumpulkan poin dan tukarkan dengan VIP gratis di menu /referal!"
                                        ),
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
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

    # Bersihkan sesi lama secara menyeluruh
    db.clear_session(user.id)
    try:
        from middleware.session import clear_user_dir
        from handlers.cancel_helper import cancel_all
        cancel_all(user.id)
        clear_user_dir(user.id)
    except Exception:
        pass

    menu_text = build_menu_text(first_name, user.id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("TUTORIAL", url=TUTORIAL_LINK, style="danger"),
            InlineKeyboardButton("DEVELOPER", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}", style="primary")
        ]
    ])

    # Coba edit pesan callback in-place agar transisi super smooth!
    edited_msg = None
    try:
        edited_msg = await query.edit_message_text(
            text=menu_text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Gagal edit_message_text di back_to_start, fallback ke send: %s", e)
        try:
            await query.message.delete()
        except Exception:
            pass

    welcome_msg_id = edited_msg.message_id if edited_msg else None

    if not welcome_msg_id:
        # Fallback: Kirim ulang msg1
        msg1 = await context.bot.send_message(
            chat_id=chat_id,
            text=menu_text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        welcome_msg_id = msg1.message_id

    _welcome_messages.pop(user.id, None)
    register_welcome_messages(user.id, [welcome_msg_id])



