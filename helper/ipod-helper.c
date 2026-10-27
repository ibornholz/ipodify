/*
 * ipod-helper — tiny libgpod frontend used by iPodify.
 *
 *   ipod-helper dump  <mount>
 *       I  model  generation  capacity_gb  artwork
 *       T  dbid  title  artist  album  albumartist  genre  track_nr  year  ms  kbps  size  plays  rating  path
 *          mediatype  podcasturl
 *       P  id  name  kind(master|podcast|smart|normal)  dbid,dbid,...
 *
 *   ipod-helper apply <mount>   < ops (one per line, tab separated)
 *       add      file  playlist  title  artist  album  genre  track_nr  year  ms  kbps  samplerate  cover
 *       addpod   file  title  artist  album  ms  kbps  samplerate  cover  released  description  guid  rss
 *       rm       dbid
 *       set      dbid  field  value      (title artist album albumartist genre composer year track_nr rating)
 *       plnew    name
 *       pldel    plid
 *       plren    plid  name
 *       pladd    plid  dbid
 *       plrm     plid  dbid
 *
 *   All ops of one apply run go into a single iTunesDB write; if any op fails nothing is written.
 *   Prints "added <dbid>" per add/addpod and "pl <id> <name>" per plnew after the write.
 */
#include <gpod/itdb.h>
#include <glib/gstdio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

static void die(const char *what, const char *msg) {
    fprintf(stderr, "%s: %s\n", what, msg ? msg : "unknown error");
    exit(2);
}

static Itdb_iTunesDB *open_db(const char *mount) {
    GError *err = NULL;
    Itdb_iTunesDB *db = itdb_parse(mount, &err);
    if (!db) die("parse", err ? err->message : NULL);
    return db;
}

/* Tabs/newlines would break the TSV output. */
static void put(const char *s) {
    for (; s && *s; s++) putchar(*s == '\t' || *s == '\n' || *s == '\r' ? ' ' : *s);
}

static Itdb_Track *track_by_id(Itdb_iTunesDB *db, const char *id) {
    guint64 dbid = g_ascii_strtoull(id, NULL, 10);
    for (GList *l = db->tracks; l; l = l->next)
        if (((Itdb_Track *)l->data)->dbid == dbid) return l->data;
    die("no such track", id);
    return NULL;
}

static Itdb_Playlist *playlist_by_id(Itdb_iTunesDB *db, const char *id) {
    Itdb_Playlist *pl = itdb_playlist_by_id(db, g_ascii_strtoull(id, NULL, 10));
    if (!pl) die("no such playlist", id);
    return pl;
}

static Itdb_Playlist *playlist_named(Itdb_iTunesDB *db, const char *name) {
    Itdb_Playlist *pl = itdb_playlist_by_name(db, (gchar *)name);
    if (!pl) {
        pl = itdb_playlist_new(name, FALSE);
        itdb_playlist_add(db, pl, -1);
    }
    return pl;
}

static int cmd_dump(const char *mount) {
    Itdb_iTunesDB *db = open_db(mount);
    const Itdb_IpodInfo *info = itdb_device_get_ipod_info(db->device);
    printf("I\t%s\t%s\t%.0f\t%d\n",
           info ? itdb_info_get_ipod_model_name_string(info->ipod_model) : "iPod",
           info ? itdb_info_get_ipod_generation_string(info->ipod_generation) : "",
           info ? info->capacity : 0.0, itdb_device_supports_artwork(db->device));

    for (GList *l = db->tracks; l; l = l->next) {
        Itdb_Track *t = l->data;
        gchar *path = itdb_filename_on_ipod(t);
        printf("T\t%llu\t", (unsigned long long)t->dbid);
        put(t->title);       putchar('\t');
        put(t->artist);      putchar('\t');
        put(t->album);       putchar('\t');
        put(t->albumartist); putchar('\t');
        put(t->genre);
        printf("\t%d\t%d\t%d\t%d\t%u\t%u\t%u\t", t->track_nr, t->year, t->tracklen, t->bitrate,
               t->size, t->playcount, t->rating / ITDB_RATING_STEP);
        put(path);
        printf("\t%u\t", t->mediatype);
        put(t->podcasturl);
        putchar('\n');
        g_free(path);
    }
    for (GList *l = db->playlists; l; l = l->next) {
        Itdb_Playlist *pl = l->data;
        const char *kind = itdb_playlist_is_mpl(pl) ? "master"
                         : itdb_playlist_is_podcasts(pl) ? "podcast"
                         : pl->is_spl ? "smart" : "normal";
        printf("P\t%llu\t", (unsigned long long)pl->id);
        put(pl->name);
        printf("\t%s\t", kind);
        for (GList *m = pl->members; m; m = m->next)
            printf("%s%llu", m == pl->members ? "" : ",", (unsigned long long)((Itdb_Track *)m->data)->dbid);
        putchar('\n');
    }
    itdb_free(db);
    return 0;
}

static void op_add(Itdb_iTunesDB *db, gchar **a, int n, GPtrArray *added) {
    if (n < 13) die("add", "missing arguments");
    const char *file = a[1], *playlist = a[2], *cover = a[12];
    struct stat st;
    if (g_stat(file, &st) != 0) die("add: cannot stat", file);

    Itdb_Track *t = itdb_track_new();
    t->title  = g_strdup(a[3]);
    t->artist = g_strdup(a[4]);
    t->album  = g_strdup(a[5]);
    t->genre  = g_strdup(a[6]);
    t->track_nr   = atoi(a[7]);
    t->year       = atoi(a[8]);
    t->tracklen   = atoi(a[9]);
    t->bitrate    = atoi(a[10]);
    t->samplerate = atoi(a[11]);
    t->size       = st.st_size;
    t->mediatype  = ITDB_MEDIATYPE_AUDIO;
    t->time_added = time(NULL);
    t->filetype   = g_strdup(g_str_has_suffix(file, ".m4a") ? "AAC audio file" : "MPEG audio file");

    itdb_track_add(db, t, -1);
    itdb_playlist_add_track(itdb_playlist_mpl(db), t, -1);
    if (*playlist) itdb_playlist_add_track(playlist_named(db, playlist), t, -1);
    if (*cover && itdb_device_supports_artwork(db->device)) itdb_track_set_thumbnails(t, cover);

    GError *err = NULL;
    if (!itdb_cp_track_to_ipod(t, file, &err)) die("copy", err ? err->message : NULL);
    g_ptr_array_add(added, t);
}

/* Podcast episode: goes into the iPod's own Podcasts menu (grouped by album = show name),
 * marked unplayed, remembers its position and is skipped when shuffling. */
static void op_addpod(Itdb_iTunesDB *db, gchar **a, int n, GPtrArray *added) {
    if (n < 13) die("addpod", "missing arguments");
    const char *file = a[1], *cover = a[8];
    struct stat st;
    if (g_stat(file, &st) != 0) die("addpod: cannot stat", file);

    Itdb_Track *t = itdb_track_new();
    t->title       = g_strdup(a[2]);
    t->artist      = g_strdup(a[3]);
    t->album       = g_strdup(a[4]);
    t->genre       = g_strdup("Podcast");
    t->tracklen    = atoi(a[5]);
    t->bitrate     = atoi(a[6]);
    t->samplerate  = atoi(a[7]);
    t->time_released = (time_t)g_ascii_strtoll(a[9], NULL, 10);
    t->description = g_strdup(a[10]);
    t->subtitle    = g_strdup(a[10]);
    t->podcasturl  = g_strdup(a[11]);
    t->podcastrss  = g_strdup(a[12]);
    t->size        = st.st_size;
    t->mediatype   = ITDB_MEDIATYPE_AUDIO | ITDB_MEDIATYPE_PODCAST;  /* 5, like iTunes */
    t->skip_when_shuffling = 0x01;
    t->remember_playback_position = 0x01;
    t->flag4       = 0x01;
    t->mark_unplayed = 0x02;
    t->time_added  = time(NULL);
    t->filetype    = g_strdup(g_str_has_suffix(file, ".m4a") ? "AAC audio file" : "MPEG audio file");

    itdb_track_add(db, t, -1);
    itdb_playlist_add_track(itdb_playlist_mpl(db), t, -1);
    Itdb_Playlist *pods = itdb_playlist_podcasts(db);
    if (!pods) {
        pods = itdb_playlist_new("Podcasts", FALSE);
        itdb_playlist_set_podcasts(pods);
        itdb_playlist_add(db, pods, -1);
    }
    itdb_playlist_add_track(pods, t, -1);
    if (*cover && itdb_device_supports_artwork(db->device)) itdb_track_set_thumbnails(t, cover);

    GError *err = NULL;
    if (!itdb_cp_track_to_ipod(t, file, &err)) die("copy", err ? err->message : NULL);
    g_ptr_array_add(added, t);
}

static void op_rm(Itdb_iTunesDB *db, const char *id) {
    Itdb_Track *t = track_by_id(db, id);
    for (GList *p = db->playlists; p; p = p->next)
        while (itdb_playlist_contains_track(p->data, t)) itdb_playlist_remove_track(p->data, t);
    gchar *path = itdb_filename_on_ipod(t);
    if (path) g_unlink(path);
    g_free(path);
    itdb_track_remove(t);
}

static void set_str(gchar **field, const char *v) {
    g_free(*field);
    *field = g_strdup(v);
}

static void op_set(Itdb_iTunesDB *db, const char *id, const char *field, const char *v) {
    Itdb_Track *t = track_by_id(db, id);
    if      (!strcmp(field, "title"))       set_str(&t->title, v);
    else if (!strcmp(field, "artist"))      set_str(&t->artist, v);
    else if (!strcmp(field, "album"))       set_str(&t->album, v);
    else if (!strcmp(field, "albumartist")) set_str(&t->albumartist, v);
    else if (!strcmp(field, "genre"))       set_str(&t->genre, v);
    else if (!strcmp(field, "composer"))    set_str(&t->composer, v);
    else if (!strcmp(field, "year"))        t->year = atoi(v);
    else if (!strcmp(field, "track_nr"))    t->track_nr = atoi(v);
    else if (!strcmp(field, "rating"))      t->rating = atoi(v) * ITDB_RATING_STEP;
    else die("set: unknown field", field);
}

static int cmd_apply(const char *mount) {
    Itdb_iTunesDB *db = open_db(mount);
    GPtrArray *added = g_ptr_array_new(), *created = g_ptr_array_new();
    char line[16384];

    while (fgets(line, sizeof line, stdin)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!*line) continue;
        gchar **a = g_strsplit(line, "\t", -1);
        int n = g_strv_length(a);
        const char *op = a[0];
        if      (!strcmp(op, "add"))                 op_add(db, a, n, added);
        else if (!strcmp(op, "addpod"))              op_addpod(db, a, n, added);
        else if (!strcmp(op, "rm") && n > 1)         op_rm(db, a[1]);
        else if (!strcmp(op, "set") && n > 3)        op_set(db, a[1], a[2], a[3]);
        else if (!strcmp(op, "plnew") && n > 1) {
            Itdb_Playlist *pl = itdb_playlist_new(a[1], FALSE);
            itdb_playlist_add(db, pl, -1);
            g_ptr_array_add(created, pl);
        }
        else if (!strcmp(op, "pldel") && n > 1) {
            Itdb_Playlist *pl = playlist_by_id(db, a[1]);
            if (itdb_playlist_is_mpl(pl)) die("pldel", "cannot delete the master playlist");
            itdb_playlist_remove(pl);
        }
        else if (!strcmp(op, "plren") && n > 2) {
            Itdb_Playlist *pl = playlist_by_id(db, a[1]);
            set_str(&pl->name, a[2]);
        }
        else if (!strcmp(op, "pladd") && n > 2) {
            Itdb_Playlist *pl = playlist_by_id(db, a[1]);
            Itdb_Track *t = track_by_id(db, a[2]);
            if (!itdb_playlist_contains_track(pl, t)) itdb_playlist_add_track(pl, t, -1);
        }
        else if (!strcmp(op, "plrm") && n > 2) {
            Itdb_Playlist *pl = playlist_by_id(db, a[1]);
            if (itdb_playlist_is_mpl(pl)) die("plrm", "use rm to delete a track from the iPod");
            itdb_playlist_remove_track(pl, track_by_id(db, a[2]));
        }
        else die("unknown op", line);
        g_strfreev(a);
    }

    GError *err = NULL;
    if (!itdb_write(db, &err)) die("write", err ? err->message : NULL);
    for (guint i = 0; i < added->len; i++)
        printf("added\t%llu\n", (unsigned long long)((Itdb_Track *)added->pdata[i])->dbid);
    for (guint i = 0; i < created->len; i++) {
        Itdb_Playlist *pl = created->pdata[i];
        printf("pl\t%llu\t%s\n", (unsigned long long)pl->id, pl->name);
    }
    itdb_free(db);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: ipod-helper dump|apply <mount>\n");
        return 1;
    }
    if (!strcmp(argv[1], "dump"))  return cmd_dump(argv[2]);
    if (!strcmp(argv[1], "apply")) return cmd_apply(argv[2]);
    fprintf(stderr, "unknown command %s\n", argv[1]);
    return 1;
}
