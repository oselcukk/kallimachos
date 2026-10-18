# Kallimachos

> **Ajanlar için:** kurulum için [INSTALL.tr.md](INSTALL.tr.md) dosyasını adım adım uygulayın (English: [INSTALL.md](INSTALL.md)).

Kallimachos tek bir kişinin araştırma literatürünü izler. Her sabah seçtiğiniz kaynaklarda
profilinize uyan yeni makaleleri tarar, süzer, sıralar ve size kısa bir mesaj gönderir
(Markdown dosyası, Telegram ya da e-posta). Birkaç kelimeyle cevap verirsiniz (`3 iyi`,
`5 ilgisiz`, `2 oku`, `7 arşivle`); Kallimachos bu cevaplardan öğrenir. İsteğe bağlı modüller
açık erişimli PDF'leri düzenli bir arşive indirir ve makaleleri ajanınızla birlikte okuyarak
doğrulanmış literatür notlarına dönüştürür. Notlar bir Obsidian klasörüne ya da bir
[Avenox](https://github.com/avenoxai/avenoxbeyin) kasasına yazılır.

Adını, İskenderiye Kütüphanesi'nin ilk kataloğu olan *Pinakes*'i hazırlayan Kyreneli
Kallimakhos'tan alır. English: [README.md](README.md).

**Kimin için:** böyle bir sistemi kendisi kurmadan günlük, kişisel bir literatür akışı isteyen
ve Claude Code, Codex ya da Hermes gibi bir kodlama ajanı kullanan (ya da kullanmaya hazır olan)
araştırmacılar ve öğrenciler için. Web arayüzü yoktur: ajanınızla konuşursunuz, ajan `kallimachos`
komutlarını çalıştırır.

## Kurulum

Ajanınıza bu reponun bağlantısını (`https://github.com/oselcukk/kallimachos`) verin ve
*"bunu kur"* deyin.

Ajan depoyu indirir, sanal ortamı kurar, sihirbazı sizin dilinizde çalıştırır, her modülü test
eder ve her onay kapısında durur (profiliniz, deneme e-postası, isteğe bağlı hook'lar, zamanlayıcı).
Python 3.11 ya da üstü; macOS, Linux ve Windows.

Elle kurmak için: `git clone https://github.com/oselcukk/kallimachos.git`,
`python3 -m venv .venv && .venv/bin/pip install -e .`, sonra klasörü ajanınızda açıp *"AGENTS.md'yi
oku ve install skill'ini çalıştır"* deyin.

## Modüller

| Modül | Ne yapar | Varsayılan |
|---|---|---|
| Tarama | arXiv, IACR ePrint, Crossref (ISSN ile dergiler), OpenAlex, Semantic Scholar, zbMATH Open, PubMed, bioRxiv/medRxiv, RSS/Atom, Google Scholar uyarı e-postaları (posta kutunuzdan okunur; Scholar'a hiçbir istek gönderilmez) | açık; kaynaklar alana göre |
| Profil | kısa bir soru-cevaptan, dosyalarınızdan (PDF klasörü, `.bib`, `.ris`, Zotero CSL-JSON, Markdown notları) ya da ORCID / OpenAlex kimliğinizden çıkarılır; ilk taramadan önce siz onaylarsınız | soru-cevap |
| Puanlama ve seçim | çekirdek terimli konu kanalları, takip edilen yazarlar ve dergiler, gerekçesi izlenebilen kurallar, kotalar; isteğe bağlı model katmanı (`claude -p`, `codex exec`) yeniden sıralar ve özetler | yalnız kurallar |
| Bildirim ve geri bildirim | Markdown günlük dosyası, Telegram, e-posta; cevaplar Telegram, e-posta ya da işaretlenen kutularla gelir; ağırlık değişiklikleri önerilir, onayınızla uygulanır | Markdown |
| Arşiv | yalnız açık erişimli PDF'ler (arXiv, yayıncı OA, Unpaywall OA), tutarlı dosya adları, olay kaydı tutan bir katalog, mevcut kütüphanenizin salt okunur taraması | kapalı |
| İşleme | ajanınızla dalgalar hâlinde okuma: metin, kart, alıntı denetimi, atıf denetimi, anlam doğrulaması, not, bağlantılar, küme özetleri; Zotero aktarımı | kapalı |
| Köprü | sunucudan bilgisayarınıza sha doğrulamalı teslim (SSH/rsync) | kapalı |
| Zamanlama | launchd (macOS), Görev Zamanlayıcı (Windows), systemd timer ya da cron (Linux), Hermes cron, GitHub Actions; kaçırılan günler tek mesajda telafi edilir | yerel, her gün 07:30, Pazar 09:30 haftalık |
| Hook'lar | isteğe bağlı Claude Code / Codex hook'ları: oturum başında kısa durum özeti ve ajanların durum dosyalarını elle değiştirmesini engelleyen bir guard | kapalı, kurulumda sorulur |

Ayrıntılar (İngilizce): [docs/sources.md](docs/sources.md),
[docs/processing.md](docs/processing.md), [docs/avenox.md](docs/avenox.md),
[docs/bridge.md](docs/bridge.md), [docs/schedule.md](docs/schedule.md),
[docs/hooks.md](docs/hooks.md), [docs/configuration.md](docs/configuration.md),
[docs/security.md](docs/security.md), [docs/troubleshooting.md](docs/troubleshooting.md).

## Gizlilik, telif ve istek nezaketi

- **Verileriniz makinenizde kalır.** Profil, geri bildirim, katalog ve çalıştırma kayıtları veri
  klasörünüzdedir (`~/Library/Application Support/kallimachos`, `~/.local/share/kallimachos` ya da
  `%LOCALAPPDATA%\kallimachos`), asla depoda değil. Log kayıtlarına ad ya da başlık değil, yalnız
  kimlikler yazılır.
- **Sırlar yalnız referans olarak tutulur.** `config.yaml` anahtarın kendisini değil,
  `keyring:kallimachos/<ad>` ya da `env:DEGISKEN` referansını tutar; `config validate` token'a
  benzeyen her değeri reddeder.
- **Gizlilik kapısı.** Dosyalarınızdan profil çıkarılırken ya da kütüphaneniz taranırken yalnız
  güçlü bir kimliği (ya da uzun, makaleye benzer bir başlığı) olan kayıtlar çevrim içi sorgulanır;
  kişisel görünen yollar (`private`, `health`, `students`, `exam`, ...) hiç açılmaz.
- **Yalnız açık erişim.** Giriş ya da ödeme duvarı arkasındaki dosyalar indirilmez, korsan
  kütüphaneler kullanılmaz; 403 hatası ya da HTML sayfası erişilemez sayılır.
- **Ölçülü istekler.** Her API için düşürülemeyen sabit en kısa aralıklar, `Retry-After`'a uyum,
  kendini tanıtan bir User-Agent, aynı anda tek OpenAlex işi ve günlük bir OpenAlex bütçesi.
- **Dosyalarınızın üzerine yazılmaz.** Var olan bir kullanıcı dosyasını değiştirecek her komut
  önce o dosyayı listeler ve siz onaylayana kadar çıkış kodu 5 ile durur.
- **Veri talimat değildir.** Başlıklar, özetler, e-postalar ve mesajlar ne kod ne de ajan
  prompt'ları tarafından komut olarak uygulanır.

## Alıntının bulunması kalitenin kanıtı değildir

İşleme modülü, alıntılanan her cümlenin gerçekten belirtilen sayfada geçtiğini denetler. Bu
denetim uydurma alıntıları yakalar; ama bir not doğru alıntı yapıp makaleyi yine de yanlış
anlayabilir. Bu yüzden ayrı bir doğrulayıcı makaleyi yeniden okur ve not yazılmadan önce anlamı
onaylamak zorundadır (`semantic-record`). Bkz. [docs/processing.md](docs/processing.md).

## Avenox ile ilişki

[Avenox (avenoxbeyin)](https://github.com/avenoxai/avenoxbeyin), bir Obsidian kasasında çalışan
ajanlar için bir hafıza katmanıdır. Kallimachos ona bağımlı değildir. Notlarınız bir Avenox
kasasındaysa Kallimachos literatür notlarını oraya Avenox kurallarına uyarak yazar (frontmatter,
klasör, ardından `beyin.py sync`; okunacaklar Avenox görevi olur) ve companion, knowledge ya da
receipt klasörlerine dokunmaz. Kallimachos'un hook'ları Avenox'un hook'larıyla birlikte çalışır ve
onları değiştirmez; Kallimachos'un receipt hook'u yoktur (oturumu Avenox kaydeder). Avenox yoksa
aynı notlar düz bir klasöre gider. Bkz. [docs/avenox.md](docs/avenox.md).

## Zotero hakkında

`process zotero`, kendi anahtar referansınızla Zotero web API'si üzerinden yazar. Çok sayıda öğeyi
değiştiren komutlar (`fix-metadata`, `split-names`, `reset-citekeys`) önce rapor verir: bunları
`--dry-run` ile çalıştırın, raporu okuyun ve bir Zotero yedeği alın. Öğeler yalnız arşiv kimliğiyle
bağlanır.

## Sorun giderme

`kallimachos doctor` komutunu çalıştırın. Komut klasörleri, config'i, profili, sırları (yalnız
var/yok), kilitleri, diski, son çalıştırmayı ve kaynaklarını, durum dosyalarını, Avenox'u,
zamanlayıcıyı ve hook'ları denetler ve ne yapmanız gerektiğini söyler. Çıkış kodları: 0 tamam,
1 beklenmeyen hata, 2 geçersiz config ya da kullanım, 3 kısmi başarı, 4 kilit başka bir çalıştırmanın
elinde, 5 onay gerekli, 6 ağ hatası ya da bütçe tükendi. Sık karşılaşılan durumlar
[docs/troubleshooting.md](docs/troubleshooting.md) dosyasında anlatılır.

## Katkı

Yeni bir kaynak eklemek için `src/kallimachos/sources/` altında `Source` protokolünü uygulayan tek
bir modül ve `tests/fixtures/<kaynak>/` altında kayıtlı fixture'lar yeterlidir; testler ağ
kullanmaz. Pull request açmadan önce: `ruff check src tests tools`,
`ruff format --check src tests tools`, `mypy src`, `pytest`. Kod, tanımlayıcılar ve commit mesajları
İngilizce yazılır; örnekler kurgusaldır.

## Lisans

MIT, bkz. [LICENSE](LICENSE).
