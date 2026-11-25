import java.io.*;
import java.nio.*;
import java.nio.file.*;
import java.time.Instant;
import java.util.*;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;
import tritechgemini.fileio.*;
import tritechgemini.imagedata.*;

/**
 * Export all Gemini GLF image records using the TritechFiles library.
 *
 * Outputs:
 *   frame_metadata.csv  - one row per image record
 *   frames_u8.bin       - concatenated raw [range,beam] uint8 matrices
 *   bearings_f64le.bin  - concatenated per-frame bearing tables (radians, float64 LE)
 *   summary.txt         - catalog summary
 *
 * Python can reconstruct frame i by reading frame_length_bytes starting at
 * frame_offset_bytes and reshaping to [n_range, n_beam].
 */
public class ExportGLF {
    static String iso(long ms) { return Instant.ofEpochMilli(ms).toString(); }

    static void writeDoubleLE(OutputStream out, double value) throws IOException {
        long bits = Double.doubleToLongBits(value);
        out.write((int)(bits       ) & 0xff);
        out.write((int)(bits >>  8) & 0xff);
        out.write((int)(bits >> 16) & 0xff);
        out.write((int)(bits >> 24) & 0xff);
        out.write((int)(bits >> 32) & 0xff);
        out.write((int)(bits >> 40) & 0xff);
        out.write((int)(bits >> 48) & 0xff);
        out.write((int)(bits >> 56) & 0xff);
    }

    static String csvSafe(String s) {
        if (s == null) return "";
        if (s.contains(",") || s.contains("\"") || s.contains("\n")) {
            return "\"" + s.replace("\"", "\"\"") + "\"";
        }
        return s;
    }

    static File extractDatFromGlf(File glfFile, File outDir) throws IOException {
        File cacheDir = new File(outDir, "_glf_cache");
        if (!cacheDir.exists() && !cacheDir.mkdirs()) {
            throw new IOException("Cannot create cache directory: " + cacheDir);
        }

        // ZipFile is strict about EOCD/central directory integrity; Tritech GLF
        // files often have malformed EOCD (e.g. offsets pointing past EOF). Use
        // ZipInputStream which walks local file headers sequentially.
        String datName = null;
        long datExpected = -1;
        try (InputStream raw = new BufferedInputStream(new FileInputStream(glfFile));
             ZipInputStream zin = new ZipInputStream(raw)) {
            ZipEntry entry;
            while ((entry = zin.getNextEntry()) != null) {
                String name = entry.getName();
                if (!entry.isDirectory()
                        && name.toLowerCase(Locale.ROOT).endsWith(".dat")) {
                    datName = new File(name).getName();
                    datExpected = entry.getSize();
                    File datFile = new File(cacheDir, datName);
                    System.out.println("Extracting embedded DAT: " + name);
                    try (OutputStream out = new BufferedOutputStream(new FileOutputStream(datFile))) {
                        byte[] buf = new byte[1 << 16];
                        int n;
                        while ((n = zin.read(buf)) > 0) {
                            out.write(buf, 0, n);
                        }
                    }
                    if (datFile.length() == 0) {
                        throw new IOException("Extracted DAT is empty: " + datFile);
                    }
                    System.out.println("DAT extracted: " + datFile.getAbsolutePath());
                    System.out.println("DAT size: " + datFile.length() + " bytes (expected " + datExpected + ")");
                    return datFile;
                }
            }
        }
        throw new IOException(
            "No .dat member could be extracted from GLF: " + glfFile
            + " (file may be truncated or encrypted; only settings_20427.xml is present)"
        );
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: java ExportGLF input.glf output_dir");
            System.exit(2);
        }

        String path = args[0];
        File inputFile = new File(path);
        File outDir = new File(args[1]);
        if (!outDir.exists() && !outDir.mkdirs()) {
            throw new IOException("Cannot create output directory: " + outDir);
        }
        if (!inputFile.exists()) {
            throw new FileNotFoundException("Input file not found: " + inputFile);
        }

        File dataFile;
        if (path.toLowerCase(Locale.ROOT).endsWith(".glf")) {
            dataFile = extractDatFromGlf(inputFile, outDir);
        } else if (path.toLowerCase(Locale.ROOT).endsWith(".dat")) {
            dataFile = inputFile;
        } else {
            throw new IllegalArgumentException("Only .glf or .dat is supported: " + path);
        }

        GeminiFileCatalog<?> cat = GeminiFileCatalog.getFileCatalog(dataFile.getAbsolutePath(), true);

        int nRecords = cat.getNumRecords();
        System.out.println("TritechFiles records: " + nRecords);
        if (nRecords <= 0) {
            throw new IOException(
                "TritechFiles parsed 0 image records from DAT: " + dataFile.getAbsolutePath()
            );
        }

        File frameBinFile = new File(outDir, "frames_u8.bin");
        File bearingBinFile = new File(outDir, "bearings_f64le.bin");
        File metaFile = new File(outDir, "frame_metadata.csv");

        long frameOffset = 0L;
        long bearingOffset = 0L;

        try (BufferedOutputStream frameOut = new BufferedOutputStream(new FileOutputStream(frameBinFile));
             BufferedOutputStream bearingOut = new BufferedOutputStream(new FileOutputStream(bearingBinFile));
             PrintWriter meta = new PrintWriter(new BufferedWriter(new FileWriter(metaFile)))) {

            meta.println("index,time_iso,time_millis,device_id,device_type,n_range,n_beam,max_range_m,sound_speed_mps,gain_percent,chirp,platform_raw,sonar_type_raw,compressed_bytes,frame_offset_bytes,frame_length_bytes,bearing_offset_bytes,bearing_length_bytes");

            for (int i = 0; i < nRecords; i++) {
                GLFImageRecord r = (GLFImageRecord) cat.getFullRecord(i);
                byte[] image = r.getImageData();
                double[] bearings = r.getBearingTable();
                if (image == null || bearings == null) {
                    r.freeImageData();
                    throw new IOException("Frame " + i + " contains null image or bearing table");
                }

                int expected = r.getnRange() * r.getnBeam();
                if (image.length != expected) {
                    r.freeImageData();
                    throw new IOException("Frame " + i + " image size mismatch: " + image.length + " vs " + expected);
                }

                frameOut.write(image);
                long frameLen = image.length;

                long bearingStart = bearingOffset;
                for (double b : bearings) {
                    writeDoubleLE(bearingOut, b);
                    bearingOffset += 8;
                }
                long bearingLen = bearings.length * 8L;

                meta.printf(Locale.US,
                    "%d,%s,%d,%d,%s,%d,%d,%.9f,%.4f,%d,%d,%d,%d,%d,%d,%d,%d,%d%n",
                    i,
                    csvSafe(iso(r.getRecordTime())),
                    r.getRecordTime(),
                    r.getDeviceId(),
                    csvSafe(r.getDeviceType()),
                    r.getnRange(),
                    r.getnBeam(),
                    r.getMaxRange(),
                    r.getSoS(),
                    r.getGain(),
                    r.getChirp(),
                    r.m_ucPlatform,
                    r.m_ucSonartype,
                    r.dataSize,
                    frameOffset,
                    frameLen,
                    bearingStart,
                    bearingLen
                );

                frameOffset += frameLen;
                r.freeImageData();
            }
        }

        try (PrintWriter pw = new PrintWriter(new BufferedWriter(new FileWriter(new File(outDir, "summary.txt"))))) {
            pw.println("input=" + path);
            pw.println("data_input=" + dataFile.getAbsolutePath());
            pw.println("catalog_class=" + cat.getClass().getName());
            pw.println("records=" + nRecords);
            pw.println("sonars=" + cat.getNumSonars());
            pw.println("sonar_ids=" + Arrays.toString(cat.getSonarIDs()));

            if (nRecords > 0) {
                long firstTime = cat.getFirstRecordTime();
                long lastTime = cat.getLastRecordTime();
                double duration = (lastTime - firstTime) / 1000.0;
                pw.println("first_time=" + iso(firstTime));
                pw.println("last_time=" + iso(lastTime));
                pw.printf(Locale.US, "duration_seconds=%.3f%n", duration);
                if (nRecords > 1 && duration > 0) {
                    pw.printf(Locale.US, "mean_fps=%.6f%n", (nRecords - 1) / duration);
                }
            }
            pw.println("frames_binary=frames_u8.bin");
            pw.println("bearings_binary=bearings_f64le.bin");
        }

        System.out.println("Export complete: " + outDir.getAbsolutePath());
        System.out.println("Frames: " + nRecords);
    }
}
