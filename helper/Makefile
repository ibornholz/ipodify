BIN := ../bin/ipod-helper

$(BIN): ipod-helper.c
	@mkdir -p ../bin
	$(CC) -O2 -Wall -o $@ $< $$(pkg-config --cflags --libs libgpod-1.0 glib-2.0 gobject-2.0)
