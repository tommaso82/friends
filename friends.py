import znc
import re
import hashlib
import random
import heapq
DEBUG = False
# =============================================================================
# Classi Ausiliarie
# =============================================================================
class QueueTimer(znc.Timer):

    def RunJob(self):
        mod = self.GetModule()
        current_tick = mod.current_tick
        mod.current_tick += 1
        mod.Log(f"{current_tick} Controllo coda - Operazioni pendenti: {len(mod.queue)}")
        # Processa tutti gli elementi la cui schedulazione è scaduta
        while mod.queue and mod.queue[0][0] <= current_tick:
            target_tick, channel_name, nick_name, mode = heapq.heappop(mod.queue)
            mod.Log(f"Processo entry: {channel_name} {nick_name} {mode}")
            network = mod.GetNetwork()
            channel = network.FindChan(channel_name)
            if not channel:
                mod.Log(f"Saltato: canale {channel_name} non trovato")
                continue
            mod.Log(f"Verifica presenza nick {nick_name} in {channel_name}")
            if not channel.FindNick(nick_name):
                mod.Log(f"Saltato: {nick_name} non più in canale")
                continue
            if not mod.IsBotOp(channel_name):
                mod.Log(f"Saltato {mode} su {nick_name}: non più OP")
                continue
            try:
                if (target_nick := channel.FindNick(nick_name)) and (
                (mode == 'o' and target_nick.HasPerm(znc.CChan.Op)) or 
                (mode == 'v' and target_nick.HasPerm(znc.CChan.Voice))
                ):
                    mod.Log(f"SKIP: {nick_name} già +{mode}")
                    continue
                mod.Log(f"Esecuzione +{mode} su {nick_name}")
                network.PutIRC(f"MODE {channel_name} +{mode} {nick_name}")
            except Exception as e:
                mod.Log(f"Errore durante +{mode}: {str(e)}")
        # Se la coda è vuota, ferma il timer
        if not mod.queue:
            mod.Log("Coda vuota - Fermo il timer")
            self.Stop()
            mod.timer = None
            mod.Log(f"Conferma stato timer: {'Fermo' if mod.timer is None else 'Attivo'}")
            mod.Log(f"ID timer corrente: {id(mod.timer) if mod.timer else 'Nessuno'}")
            mod.current_tick = 0
            mod.Log(f"variabile tick resettata ->{current_tick}")
            return znc.HALT
        return znc.CONTINUE

class FriendUser:

    def __init__(self, handle="", password="", hostmasks="", flags=""):
        self.handle = handle
        self.password_hash = self.hash_password(password)
        self.hostmasks = set(hostmasks.split(',')) if hostmasks else set()
        self.channel_settings = self.parse_settings(flags)

    @staticmethod
    def hash_password(password):
        return hashlib.sha256(password.encode()).hexdigest()

    def verify_password(self, password):
        return self.password_hash == self.hash_password(password)

    def parse_settings(self, flags_str):
        settings = {}
        if not flags_str:
            return settings
        for item in flags_str.split(','):
            # Parte iniziale: estrazione canale e componenti
            chan = '*'
            flag_part = item
            delay = None
            # Split channel se presente (es: #chan:flags@delay)
            if ':' in item:
                chan_part, flag_part = item.split(':', 1)
                chan = chan_part.lower()
            # Estrazione delay e flag
            if '@' in flag_part:
                flag_part, delay_part = flag_part.split('@', 1)
                try:
                    delay = int(delay_part)
                except ValueError:
                    pass  # Ignora delay non valido
            # Costruzione entry
            settings[chan] = {
                'flags': set(flag_part),
                'delay': delay
            }
        return settings

    def get_delay(self, channel):
        channel_lower = channel.lower()
        # Cerca prima il canale specifico, poi globale
        for chan in [channel_lower, '*']:
            if chan in self.channel_settings:
                delay = self.channel_settings[chan].get('delay')
                if delay is not None:
                    return delay
        return random.randint(10, 60)

    def to_string(self):
        items = []
        for chan, data in self.channel_settings.items():
            entry = ""
            if chan == '*':
                entry = f"{''.join(data['flags'])}"
            else:
                entry = f"{chan}:{''.join(data['flags'])}"
            if data['delay'] is not None:
                entry += f"@{data['delay']}"
            items.append(entry)
        return f"{self.handle}\t{self.password_hash}\t{','.join(self.hostmasks)}\t{','.join(items)}"

    @classmethod
    def from_string(cls, line):
        parts = line.split('\t')
        if len(parts) != 4:
            return None
        user = cls(handle=parts[0], password="", hostmasks=parts[2], flags=parts[3])
        user.password_hash = parts[1]
        return user

# =============================================================================
# Modulo Principale
# =============================================================================

class friends(znc.Module):
    description = "Advanced Friend System with CTCP Handling"
    module_types = [znc.CModInfo.NetworkModule]

    # -------------------------------------------------------------------------
    # Inizializzazione e Logging
    # -------------------------------------------------------------------------

    def OnLoad(self, args, message):
        # Inizializza il modulo e carica gli utenti
        self.users = {}
        self.LoadUsers()
        #self.PutModule("=== System initialized ===")
        #added#
        self.queue = []
        self.timer = None
        self.current_tick = 0  # contatore tick
        self.PutModule("""🖥️  \x02\x1f(Almost) Friends 1.0\x02\x1f  🖥️
├ Config: /msg *friends help
└ Contact: 📧 tom@tom.mk""")
        return True

    def Log(self, message):
        if not DEBUG:
            return
        # Funzione di logging per il modulo
        self.PutModule(f"[DEBUG] {message}")
    # -------------------------------------------------------------------------
    # Gestione Utenti (Caricamento/Salvataggio)
    # -------------------------------------------------------------------------

    def LoadUsers(self):
        # Carica gli utenti dal NV storage
        try:
            self.users.clear()
            for key in self.nv:
                if key.startswith("channel_key_"):
                    continue  # Skip eventuali chiavi precedenti (ora obsolete)
                user = FriendUser.from_string(self.nv[key])
                if user:
                    self.users[user.handle.lower()] = user
            self.Log(f"Loaded users: {len(self.users)}")
        except Exception as e:
            self.Log(f"Load error: {str(e)}")

    def SaveUser(self, user):
        # Salva l'utente nel NV storage
        self.nv[user.handle.lower()] = user.to_string()
        self.Log(f"Saved user: {user.handle}")

    # -------------------------------------------------------------------------
    # Autenticazione e Controllo Permessi
    # -------------------------------------------------------------------------

    def Authenticate(self, nick, password):
        # Autentica l'utente confrontando la hostmask e la password
        try:
            hostmask = nick.GetHostMask()
            self.Log(f"Auth check for {hostmask}")
            for user in self.users.values():
                for pattern in user.hostmasks:
                    if self.MatchHostmask(hostmask, pattern):
                        self.Log(f"Hostmask match: {user.handle}")
                        if user.password_hash and not user.verify_password(password):
                            self.Log("Password mismatch")
                            return None
                        return user
            return None
        except Exception as e:
            self.Log(f"Auth error: {str(e)}")
            return None

    def MatchHostmask(self, hostmask, pattern):
        try:
            #self.Log(f"Matching: {hostmask} vs {pattern}")
            regex_pattern = '^' + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + '$'
            #self.Log(f"Regex generata: {regex_pattern}")
            match = re.fullmatch(regex_pattern, hostmask, re.IGNORECASE)
            self.Log(f"Risultato match: {bool(match)}")
            return match
        except Exception as e:
            self.Log(f"!ERRORE in MatchBanPattern: {str(e)}")
            return False

    def CheckPermission(self, user, channel, flag):
        channel_lower = channel.lower()
        # Cerca in: canale specifico -> globale -> nessuno
        for scope in [channel_lower, '*']:
            if scope in user.channel_settings:
                if flag in user.channel_settings[scope]['flags']:
                    return True
        return False
    # -------------------------------------------------------------------------
    # Gestione Canali e delayed Auto-Op/Voice
    # -------------------------------------------------------------------------

    def IsBotOp(self, channel_name):
        return bool(
            self.GetNetwork().FindChan(channel_name) 
            and self.GetNetwork().FindChan(channel_name).HasPerm(znc.CChan.Op)
        )

    def GetChannelKey(self, channel_name):
        try:
            if chan := self.GetNetwork().FindChan(channel_name):
                return chan.GetKey()
            self.Log(f"Key: {channel_name} not found")
            return None
        except Exception as e:
            self.Log(f"Key error: {str(e)}")
            return None

    def GetChannelLimit(self, channel_name):
        try:
            if chan := self.GetNetwork().FindChan(channel_name):
                return int(chan.GetModeArg("l"))
            self.Log(f"Limit: {channel_name} not found")
            return None
        except Exception as e:
            self.Log(f"Limit error: {str(e)}")
            return None

    def ScheduleAutoMode(self, channel, nick, mode, delay):
        self.Log(f"[DEBUG] Stato timer prima di schedulare: {self.timer} (ID: {id(self.timer)})")
        try:
            current_tick = self.current_tick
            target_tick = current_tick + int(delay-1)
            channel_name = channel.GetName()
            nick_name = nick.GetNick()
            # Controllo duplicati
            for entry in self.queue:
                if entry[1] == channel_name and entry[2] == nick_name:
                    self.Log(f"Operazione già in coda per {nick_name} su {channel_name}.")
                    return
            new_entry = (target_tick, channel_name, nick_name, mode)
            self.Log(f"Nuova operazione in coda: {new_entry}")
            # Inserisce l'elemento nella coda prioritaria
            heapq.heappush(self.queue, new_entry)
            # Se il timer non è attivo, lo avvia
            if not (self.timer and self.timer.isValid()):
                self.Log(">>> Avvio timer <<<")
                self.timer = self.CreateTimer(
                    QueueTimer,
                    interval=1,
                    cycles=0,
                    label="DelayOpQueueTimer"
                )
                self.Log(f"[DEBUG] Timer attivo: {self.timer} (ID: {id(self.timer)})")
        except Exception as e:
            self.Log(f"Errore schedulazione: {str(e)}")

    def OnJoin(self, nick, channel):
        try:
            if nick.GetNick() == self.GetNetwork().GetCurNick():
                self.Log(f"non processo il mio join")
                return znc.CONTINUE
            hostmask = nick.GetHostMask()
            channel_name = channel.GetName()
            network = self.GetNetwork()
            for user in self.users.values():
                if not any(self.MatchHostmask(hostmask, hm) for hm in user.hostmasks):
                    continue
                # Determina la modalità
                mode = None
                if self.CheckPermission(user, channel_name, 'a'):
                    mode = 'o' if self.CheckPermission(user, channel_name, 'o') \
                        else 'v' if self.CheckPermission(user, channel_name, 'v') \
                        else None
                if not mode:
                    return znc.CONTINUE  # Early exit
                if not self.IsBotOp(channel_name):
                    self.Log(f"Saltato auto-{mode}: non OP in {channel_name}")
                    continue  # Exit
                delay = user.get_delay(channel_name)
                # Gestione delay=0
                if delay <= 0:
                    try:
                        if network and channel.FindNick(nick.GetNick()):
                            network.PutIRC(f"MODE {channel_name} +{mode} {nick.GetNick()}")
                            self.Log(f"[IMMEDIATE] +{mode} su {nick.GetNick()}")
                    except Exception as e:
                        self.Log(f"Errore op immediata: {str(e)}")
                    return znc.CONTINUE  # Processato, esci
                # Caso normale
                self.ScheduleAutoMode(channel, nick, mode, delay)
                self.Log(f"Schedulato +{mode} tra {delay}s")
                return znc.CONTINUE  # Early exit dopo processamento
            return znc.CONTINUE
        except Exception as e:
            self.Log(f"Errore OnJoin: {str(e)}")
            return znc.CONTINUE

    # -------------------------------------------------------------------------
    # Gestione CTCP (Canale e Privato)
    # -------------------------------------------------------------------------

    def OnChanCTCP(self, nick, channel, message):
        try:
            self.Log(f"Channel CTCP from {nick.GetHostMask()} on {channel.GetName()}: {message.s}")
            return self.HandleCTCP(nick, channel, message)
        except Exception as e:
            self.Log(f"Channel CTCP error: {str(e)}")
            return znc.CONTINUE

    def OnPrivCTCP(self, nick, message):
        try:
            self.Log(f"Private CTCP from {nick.GetHostMask()}: {message.s}")
            return self.HandleCTCP(nick, None, message)
        except Exception as e:
            self.Log(f"Private CTCP error: {str(e)}")
            return znc.CONTINUE

    def HandleCTCP(self, nick, channel, message):
        # Gestisce il CTCP: estrae l'azione, identifica e richiama il gestore appropriato
        try:
            ctcp_body = message.s
            self.Log(f"Processing CTCP (raw): [{ctcp_body}]")
            parts = ctcp_body.split(' ', 1)
            action = parts[0].upper() if parts else ''
            args = parts[1].split() if len(parts) > 1 else []
            self.Log(f"Decoded CTCP - Action: {action} | Args: {args}")
            handler = getattr(self, f"Handle_{action}", None)
            if not handler:
                self.Log(f"No handler for action: {action}")
                return znc.CONTINUE
            return handler(nick, args, channel)
        except Exception as e:
            self.Log(f"CTCP processing error: {str(e)}")
            return znc.CONTINUE

    def Handle_OP(self, nick, args, channel):
        try:
            self.Log("Starting OP handling")
            if not args:
                return self.CtcpReply(nick, "ERR Missing channel")
            target_channel = args[0]
            password = ' '.join(args[1:]) if len(args) > 1 else ""
            user = self.Authenticate(nick, password)
            if not user:
                return self.CtcpReply(nick, "ERR Auth failed")
            if not self.CheckPermission(user, target_channel, 'o'):
                return self.CtcpReply(nick, "ERR No permission")
            if not self.IsBotOp(target_channel):
                return self.CtcpReply(nick, "ERR I m not OP there")
            self.PutIRC(f"MODE {target_channel} +o {nick.GetNick()}")
            return self.CtcpReply(nick, f"OK Opped on {target_channel}")
        except Exception as e:
            self.Log(f"OP error: {str(e)}")
            return self.CtcpReply(nick, f"ERR {str(e)}")

    def Handle_VOICE(self, nick, args, channel):
        try:
            self.Log("Starting VOICE handling")
            if not args:
                return self.CtcpReply(nick, "ERR Missing channel")
            target_channel = args[0]
            password = ' '.join(args[1:]) if len(args) > 1 else ""
            user = self.Authenticate(nick, password)
            if not user:
                return self.CtcpReply(nick, "ERR Auth failed")
            if not self.CheckPermission(user, target_channel, 'v'):
                return self.CtcpReply(nick, "ERR No permission")
            if not self.IsBotOp(target_channel):
                return self.CtcpReply(nick, "ERR I m not OP there")
            self.PutIRC(f"MODE {target_channel} +v {nick.GetNick()}")
            return self.CtcpReply(nick, f"OK Voiced on {target_channel}")
        except Exception as e:
            self.Log(f"VOICE error: {str(e)}")
            return self.CtcpReply(nick, f"ERR {str(e)}")

    def Handle_INVITE(self, nick, args, channel):
        try:
            self.Log("Starting INVITE handling")
            if not args:
                return self.CtcpReply(nick, "ERR Missing channel")
            target_channel = args[0]
            password = ' '.join(args[1:]) if len(args) > 1 else ""
            user = self.Authenticate(nick, password)
            if not user:
                return self.CtcpReply(nick, "ERR Auth failed")
            if not self.CheckPermission(user, target_channel, 'i'):
                return self.CtcpReply(nick, "ERR No permission")
            nick_name = nick.GetNick()
            if not self.IsBotOp(target_channel):
                return self.CtcpReply(nick, "ERR I m not OP there")
            self.Log(f"Sending INVITE command: INVITE {nick_name} {target_channel}")
            self.PutIRC(f"INVITE {nick_name} {target_channel}")
            return self.CtcpReply(nick, f"OK Invited to {target_channel}")
        except Exception as e:
            self.Log(f"INVITE error: {str(e)}")
            return self.CtcpReply(nick, f"ERR {str(e)}")

    def Handle_UNBAN(self, nick, args, channel):
        try:
            self.Log("=== UNBAN HANDLER STARTED ===")
            self.Log(f"[FASE 1] Args received: {args}")
            if not args:
                self.Log("!ERRORE: Mancanza argomenti")
                return self.CtcpReply(nick, "ERR Missing channel")
            target_channel = args[0]
            password = ' '.join(args[1:]) if len(args) > 1 else ""
            self.Log(f"[FASE 1] Target: {target_channel}, Pass: {'***' if password else '<none>'}")
            self.Log("[FASE 2] Starting authentication")
            user = self.Authenticate(nick, password)
            if not user:
                self.Log("!ERRORE: Autenticazione fallita")
                return self.CtcpReply(nick, "ERR Auth failed")
            self.Log(f"[FASE 2] Autenticato: {user.handle}")
            self.Log(f"[FASE 3] Checking 'u' flag for {target_channel}")
            if not self.CheckPermission(user, target_channel, 'u'):
                self.Log("!ERRORE: Permessi insufficienti")
                return self.CtcpReply(nick, "ERR No permission")
            self.Log("[FASE 3] Permessi confermati")
            if not self.IsBotOp(target_channel):
                return self.CtcpReply(nick, "ERR I m not OP there")
            hostmask = nick.GetHostMask()
            self.Log(f"[FASE 4] Hostmask ottenuta: {hostmask}")
            self.Log("[FASE 5] Starting unban process")
            self.Log(f"Inviando MODE {target_channel} +b")
            self.PutIRC(f"MODE {target_channel} +b")
            self.unban_data = {
                'channel': target_channel.lower(),
                'hostmask': hostmask,
                'nick': nick.GetNick(),
                'bans': [],
                'active': True,
                'mode_sent': 'timestamp'
            }
            self.Log(f"[FASE 5] Stato inizializzato: {self.unban_data}")
            return znc.HALT
        except Exception as e:
            self.Log(f"!ERRORE CRITICO: {str(e)}")
            return self.CtcpReply(nick, f"ERR Internal error: {str(e)}")

    def Handle_LIST(self, nick, args, channel):
        try:
            self.Log("Starting LIST handling")
            password = ' '.join(args) if args else ""
            user = self.Authenticate(nick, password)
            if not user:
                return self.CtcpReply(nick, "ERR Auth failed")
            channels = []
            for chan, data in user.channel_settings.items():  # <-- Cambiato "flags" in "data"
                flags_str = ''.join(sorted(data['flags'])) or "no-flags"  # <-- Estrai le flag dal dizionario
                delay_str = f"@{data['delay']}" if data['delay'] is not None else ""
                if chan == '*':
                    channels.append(f"Global:{flags_str}{delay_str}")
                else:
                    channels.append(f"{chan}:{flags_str}{delay_str}")
            if not channels:
                return self.CtcpReply(nick, "OK No channels")
            return self.CtcpReply(nick, f"OK {', '.join(channels)}")
        except Exception as e:
            self.Log(f"LIST error: {str(e)}")
            return self.CtcpReply(nick, f"ERR {str(e)}")

    def Handle_AUTH(self, nick, args, channel):
        try:
            if len(args) < 2:
                return self.CtcpReply(nick, "ERR Usage: AUTH <oldpass> <newpass>")
            old_pass, new_pass = args[0], args[1]
            user = self.Authenticate(nick, old_pass)
            if not user:
                return self.CtcpReply(nick, "ERR Current password invalid")
            # Aggiorna hash e salva
            user.password_hash = user.hash_password(new_pass)
            self.SaveUser(user)
            return self.CtcpReply(nick, "OK Password updated successfully")

        except Exception as e:
            return self.CtcpReply(nick, f"ERR {str(e)}")

    def Handle_KEY(self, nick, args, channel):
        try:
            self.Log("Starting KEY handling")
            if not args:
                return self.CtcpReply(nick, "ERR Missing channel")
            target_channel = args[0]
            password = ' '.join(args[1:]) if len(args) > 1 else ""
            user = self.Authenticate(nick, password)
            if not user:
                return self.CtcpReply(nick, "ERR Auth failed")
            if not self.CheckPermission(user, target_channel, 'k'):
                return self.CtcpReply(nick, "ERR No permission")
            channel_key = self.GetChannelKey(target_channel)
            if not channel_key:
                return self.CtcpReply(nick, f"ERR No key found for {target_channel}")
            return self.CtcpReply(nick, f"Channel key for {target_channel} is: {channel_key}")
        except Exception as e:
            self.Log(f"KEY error: {str(e)}")
            return self.CtcpReply(nick, f"ERR {str(e)}")

    def Handle_LIMIT(self, nick, args, channel):
        try:
            self.Log("Starting LIMIT handling")
            if not args:
                return self.CtcpReply(nick, "ERR Missing channel")
            target_channel = args[0]
            password = " ".join(args[1:]) if len(args) > 1 else ""
            user = self.Authenticate(nick, password)
            if not user:
                return self.CtcpReply(nick, "ERR Auth failed")
            if not self.CheckPermission(user, target_channel, 'l'):
                return self.CtcpReply(nick, "ERR No permission")
            if not self.IsBotOp(target_channel):
                return self.CtcpReply(nick, "ERR I m not OP there")
            net = self.GetNetwork()
            if not net or not net.FindChan(target_channel):
                return self.CtcpReply(nick, f"ERR Channel {target_channel} not found")
            if (current_limit := self.GetChannelLimit(target_channel)) is None:
                return self.CtcpReply(nick, "ERR Channel limit not set or invalid")
           #current_limit = self.GetChannelLimit(target_channel)
            new_limit = current_limit + 1 if current_limit is not None else 1
            self.Log(f"Setting new limit for {target_channel}: {new_limit}")
            net.PutIRC(f"MODE {target_channel} +l {new_limit}")
            return self.CtcpReply(nick, f"OK Channel limit increased to {new_limit}")
        except Exception as e:
            self.Log(f"LIMIT error: {e}")
            return self.CtcpReply(nick, f"ERR {e}")

    # -------------------------------------------------------------------------
    # Invio Risposte CTCP
    # -------------------------------------------------------------------------

    def CtcpReply(self, nick, message):
        # Invia una risposta CTCP formattata correttamente tramite NOTICE
        try:
            if isinstance(nick, znc.CNick):
                target = nick.GetNick()
            else:
                target = str(nick)
            self.PutIRC(f"NOTICE {target} :\x01{message}\x01")
            return znc.HALT
        except Exception as e:
            self.Log(f"Reply error: {str(e)}")
            return znc.HALT

    # -------------------------------------------------------------------------
    # Gestione Messaggi RAW e Procedura Unban
    # -------------------------------------------------------------------------

    def OnRaw(self, msg):
        try:
            raw = msg.s
            parts = raw.split()
            cmd = parts[1] if len(parts) > 1 else ''
            channel = parts[3].lstrip(':').lower() if len(parts) > 3 else ''
            ban_mask = parts[4] if len(parts) > 4 else ''
            if cmd == "367" and self.unban_data and channel == self.unban_data['channel']:
                self.unban_data['bans'].append(ban_mask)
            elif cmd == "368" and self.unban_data and channel == self.unban_data['channel']:
                self.ProcessUnban()
                self.unban_data['active'] = False
            return znc.CONTINUE
        except Exception as e:
            self.Log(f"!ERRORE in OnRaw: {str(e)}")
            return znc.CONTINUE

    def ProcessUnban(self):
        try:
            self.Log("=== PROCESSUNBAN START ===")
            if not self.unban_data['bans']:
                self.Log("Nessun ban da processare")
                self.CtcpReply(self.unban_data['nick'], "INFO No bans found")
                return
            self.Log(f"Inizio confronto di {self.unban_data['hostmask']} con {len(self.unban_data['bans'])} ban")
            matched_bans = []
            for i, ban in enumerate(self.unban_data['bans']):
                self.Log(f"[BAN {i+1}] Testing: {ban}")
                try:
                    if self.MatchHostmask(self.unban_data['hostmask'], ban):
                        self.Log(f"+++ MATCH con {ban}")
                        matched_bans.append(ban)
                    else:
                        self.Log("--- NO MATCH")
                except Exception as e:
                    self.Log(f"!ERRORE nel matching: {str(e)}")
            self.Log(f"Trovati {len(matched_bans)} ban corrispondenti")
            if not matched_bans:
                self.Log("Nessun match trovato")
                self.CtcpReply(self.unban_data['nick'], "INFO No matching bans")
                return
            self.Log("Invio comandi di unban...")
            for ban in matched_bans:
                cmd = f"MODE {self.unban_data['channel']} -b {ban}"
                self.Log(f"Inviando: {cmd}")
                self.PutIRC(cmd)
                self.Log("Comando inviato")
            self.CtcpReply(self.unban_data['nick'], f"OK Removed {len(matched_bans)} bans")
            self.Log("Procedura completata con successo")
        except Exception as e:
            self.Log(f"!ERRORE in ProcessUnban: {str(e)}")
            self.CtcpReply(self.unban_data['nick'], "ERR Processing failed")

    # -------------------------------------------------------------------------
    # Gestione Comandi del Modulo
    # -------------------------------------------------------------------------

    def OnModCommand(self, command):
        try:
            self.Log(f"Module command: {command}")
            args = command.strip().split()
            if not args:
                return self.ShowHelp()
            cmd = args[0].lower()
            handler = {
                'adduser': self.CmdAddUser,
                'deluser': self.CmdDelUser,
                                'addhost': self.CmdAddHost,
                'delhost': self.CmdDelHost,
                'list': self.CmdList,
                'setflags': self.CmdSetFlags,
                'help': self.ShowHelp,
                'setdelay': self.CmdSetDelay,
            }.get(cmd)
            if handler:
                handler(args[1:])
            else:
                self.PutModule("Unknown command")
        except Exception as e:
            self.Log(f"Command error: {str(e)}")
            self.PutModule(f"ERR {str(e)}")

    def CmdAddUser(self, args):
        # Aggiunge un nuovo utente
        if len(args) < 3:
            raise ValueError("Usage: adduser <handle> <password> <hostmask> [flags]")
        handle, pwd, hostmask = args[:3]
        flags = ' '.join(args[3:]) if len(args) > 3 else ""
        if handle.lower() in self.users:
            raise ValueError("User exists")
        new_user = FriendUser(
            handle=handle,
            password=pwd,
            hostmasks=hostmask,
            flags=flags
        )
        self.users[handle.lower()] = new_user
        self.SaveUser(new_user)
        self.PutModule(f"OK Added {handle}")

    def CmdDelUser(self, args):
        # Rimuove un utente esistente
        if len(args) < 1:
            raise ValueError("Usage: deluser <handle>")
        handle = args[0]
        if handle.lower() not in self.users:
            raise ValueError("User not found")
        del self.users[handle.lower()]
        del self.nv[handle.lower()]
        self.PutModule(f"OK Removed {handle}")

    def CmdAddHost(self, args):
        if len(args) < 2:
            self.PutModule("Usage: addhost <handle> <hostmask>")
            return

        handle = args[0].lower()
        new_hostmask = args[1].lower()  # Normalizza in lowercase

        if not (user := self.users.get(handle)):
            self.PutModule(f"ERR User '{handle}' not found")
            return

        if any(self.MatchHostmask(new_hostmask, hm) for hm in user.hostmasks):
            self.PutModule(f"ERR Hostmask pattern already exists for {handle}")
            return

        user.hostmasks.add(new_hostmask)
        self.SaveUser(user)
        self.PutModule(f"OK Added hostmask: {new_hostmask}")

    def CmdDelHost(self, args):
        if len(args) < 2:
            self.PutModule("Usage: delhost <handle> <hostmask>")
            return

        handle = args[0].lower()
        target_hostmask = args[1].lower()

        if not (user := self.users.get(handle)):
            self.PutModule(f"ERR User '{handle}' not found")
            return

        if target_hostmask not in user.hostmasks:
            self.PutModule(f"ERR Hostmask not found for {handle}")
            return

        user.hostmasks.discard(target_hostmask)
        self.SaveUser(user)
        self.PutModule(f"OK Removed hostmask: {target_hostmask}")

    def CmdList(self, args):
        try:
            # NEW: Gestione filtro per utente specifico
            filter_user = args[0].lower() if args else None
            filtered_users = []
            if filter_user:
                # Cerca per nome utente (case-insensitive)
                for user in self.users.values():
                    if user.handle.lower() == filter_user:
                        filtered_users.append(user)
                        break
            else:
                # Mostra tutti gli utenti
                filtered_users = self.users.values()
            if not filtered_users:
                self.PutModule("User not found" if filter_user else "No registered users")
                return
            lines = []
            for user in filtered_users:
                lines.append("=================================================")
                lines.append(f"User: \x02{user.handle}\x02 - Hostmasks: {', '.join(user.hostmasks)}")
                # Mostra impostazioni per canale
                for chan, settings in user.channel_settings.items():
                    flags = ''.join(sorted(settings['flags'])) or 'none'
                    delay = f"{settings['delay']}s" if settings['delay'] is not None else "random"
                    if chan == '*':
                        lines.append(f"  Global     -> Flags: {flags.ljust(5)} | Delay: {delay}")
                    else:
                        lines.append(f"  {chan.ljust(10)} -> Flags: {flags.ljust(5)} | Delay: {delay}")
                # Mostra se non ci sono impostazioni
                if not user.channel_settings:
                    lines.append("  No channel settings configured")
            lines.append("=================================================")
            self.PutModule("\n".join(lines))
        except Exception as e:
            self.Log(f"List error: {str(e)}")
            self.PutModule("ERR " + str(e))

    def CmdSetFlags(self, args):
        if len(args) < 3:
            raise ValueError("Usage: setflags <handle> <channel> [+-][flags][@delay] or -- to erase")
        handle = args[0].lower()
        raw_channel = args[1].lstrip('#').lower()
        channel = f"#{raw_channel}"
        flag_str = ''.join(args[2:])
        if not (user := self.users.get(handle)):
            self.PutModule(f"ERR User '{handle}' not found")
            return
        # Cancellazione esplicita
        if flag_str == "--":
            if channel in user.channel_settings:
                del user.channel_settings[channel]
                self.SaveUser(user)
                self.PutModule(f"OK Removed all settings for {channel}")
            else:
                self.PutModule(f"ERR No settings found for {channel}")
            return
        # Parsing avanzato con regex
        delay_match = re.search(r'@(\d+)$', flag_str)
        delay = int(delay_match.group(1)) if delay_match and delay_match.group(1).isdigit() else None
        flag_str = re.sub(r'@\d+$', '', flag_str)
        # Inizializzazione sicura
        chan_data = user.channel_settings.setdefault(channel, {'flags': set(), 'delay': None})
        current_flags = chan_data['flags']
        mode = None
        # Processamento flags ottimizzato
        for char in flag_str:
            if char == '+':
                mode = 'add'
            elif char == '-':
                mode = 'remove'
            elif mode and char in 'aoviukl':
                current_flags.add(char) if mode == 'add' else current_flags.discard(char)
        # Auto-cleanup se nessun flag
        if not current_flags:
            chan_data['delay'] = None  # Force reset delay
        # Aggiornamento delay solo se specificato
        if delay is not None:
            chan_data['delay'] = delay
        # Auto-pulizia finale
        if not current_flags and chan_data['delay'] is None:
            del user.channel_settings[channel]
        self.SaveUser(user)
        feedback = [
            f"Flags: {''.join(sorted(current_flags)) or 'none'}",
            f"Delay: {chan_data['delay']}s" if chan_data['delay'] is not None else "No delay"
        ]
        self.PutModule(f"OK {channel} updated - {', '.join(feedback)}")

    def CmdSetDelay(self, args):
        if len(args) < 2:
            raise ValueError("Usage: setdelay <handle> <channel> [seconds|delete]")

        handle = args[0]
        raw_channel = args[1].lstrip('#').lower()
        channel = f"#{raw_channel}"

        user = self.users.get(handle.lower())
        if not user:
            raise ValueError("User not found")

        # Verifica se il canale esiste già nelle impostazioni dell'utente
        if channel not in user.channel_settings:
            self.PutModule(f"Channel settings not found for {channel}. Use setflags to create an entry.")
            return  # Esci dalla funzione se il canale non esiste

        # Gestione comando delay
        if len(args) >= 3:
            delay_arg = args[2].lower()
            if delay_arg == "delete":
                delay = None
            else:
                try:
                    delay = int(args[2])
                    if delay < 0:
                        raise ValueError("Delay cannot be negative")
                except ValueError:
                    raise ValueError("Invalid delay value. Use an integer for seconds or 'delete'.")
        else:
            delay = None  # Caso di "delete" implicito se mancano argomenti dopo handle e channel

        user.channel_settings[channel]['delay'] = delay
        self.SaveUser(user)
        self.PutModule(f"Delay updated for {channel}")

    def ShowHelp(self, *args):
        help_msg = """
=== [ Friends Manager Help ] ===

\x02\x1f[ Module Commands ]\x02\x1f (via ZNC console)
  adduser <handle> <password> <hostmask> [flags] - Create new user
  deluser <handle>                               - Delete user
  addhost <handle> <hostmask>                    - Add hostmask to user
  delhost <handle> <hostmask>                    - Remove hostmask from user
  list [handle]                                  - List users/details
  setflags <handle> <chan/*> <+->[flags][@delay] - Set permissions
  setdelay <handle> <chan/*> [sec|delete]        - Set autop/voice delay
  help                                           - Show this message

\x02\x1f[ CTCP Commands ]\x02\x1f
  AUTH <oldpass> <newpass>         - Change password
  OP <channel> [password]          - Request OP
  VOICE <channel> [password]       - Request VOICE
  INVITE <channel> [password]      - Request INVITE
  UNBAN [channel] [password]       - Request UNBAN
  KEY <channel> [password]         - Get channel key
  LIMIT <channel> [password]       - Increase channel limit
  LIST [password]                  - List your permissions

\x02\x1f[ Flag Meanings ]\x02\x1f
  o = OP privileges         v = VOICE privileges
  i = INVITE privileges     u = UNBAN privileges
  k = KEY access            l = LIMIT control
  a = Auto-OP/VOICE on join

\x02\x1f[ Examples ]\x02\x1f
  # Configuration
  adduser john secret123 *!*@isp.com o@30
  setflags john #test +voi@20        - Auto-OP+VOICE+INVITE after 20s on #test
  setdelay john * delete             - Disable global delay
  # CTCP Usage
  /ctcp znc AUTH secret123 newpass456   - Change password
  /ctcp znc OP #test                    - Request OP
  /ctcp znc KEY #secretroom             - Get channel key

\x02\x1f[ Notes ]
  - setflags john #chan -- Remove channel settings
  - setflags john * --     Remove global settings
    """
        self.PutModule(help_msg)
