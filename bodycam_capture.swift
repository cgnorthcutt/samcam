import Foundation
import AVFoundation
import CoreImage
import CoreMedia
import CoreVideo
import Darwin

private let videoDeviceName = "GENERAL - UVC"
private let audioDeviceName = "GENERAL - AUDIO"
private let outputSampleRate = 16_000.0

private final class Diagnostics {
    private let lock = NSLock()

    func log(_ message: String) {
        let line = "[bodycam_capture] \(message)\n"
        guard let data = line.data(using: .utf8) else { return }

        lock.lock()
        FileHandle.standardError.write(data)
        lock.unlock()
    }
}

private let diagnostics = Diagnostics()

private enum CaptureFailure: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let message):
            return message
        }
    }
}

private final class PacketWriter {
    private let lock = NSLock()

    func write(type: UInt8, payload: Data) {
        guard payload.count <= Int(UInt32.max) else {
            diagnostics.log("Dropping oversized packet (\(payload.count) bytes)")
            return
        }

        var packet = Data(capacity: 5 + payload.count)
        packet.append(type)
        var networkLength = UInt32(payload.count).bigEndian
        Swift.withUnsafeBytes(of: &networkLength) { bytes in
            packet.append(contentsOf: bytes)
        }
        packet.append(payload)

        lock.lock()
        defer { lock.unlock() }

        var writeError: Int32?
        packet.withUnsafeBytes { rawBuffer in
            guard let baseAddress = rawBuffer.baseAddress else { return }
            var offset = 0

            while offset < rawBuffer.count {
                let result = Darwin.write(
                    STDOUT_FILENO,
                    baseAddress.advanced(by: offset),
                    rawBuffer.count - offset
                )

                if result > 0 {
                    offset += result
                } else if result < 0 && errno == EINTR {
                    continue
                } else {
                    writeError = errno
                    break
                }
            }
        }

        if let writeError {
            diagnostics.log("stdout write failed: \(String(cString: strerror(writeError)))")
            Darwin.exit(writeError == EPIPE ? EXIT_SUCCESS : EXIT_FAILURE)
        }
    }
}

private final class BodycamCapture:
    NSObject,
    AVCaptureVideoDataOutputSampleBufferDelegate,
    AVCaptureAudioDataOutputSampleBufferDelegate
{
    private let session = AVCaptureSession()
    private let videoOutput = AVCaptureVideoDataOutput()
    private let audioOutput = AVCaptureAudioDataOutput()
    private let videoQueue = DispatchQueue(label: "bodycam.capture.video", qos: .userInteractive)
    private let audioQueue = DispatchQueue(label: "bodycam.capture.audio", qos: .userInteractive)
    private let packetWriter = PacketWriter()
    private let imageContext = CIContext(options: [.cacheIntermediates: false])
    private let jpegColorSpace = CGColorSpaceCreateDeviceRGB()
    private let targetAudioFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: outputSampleRate,
        channels: 1,
        interleaved: true
    )!

    private var audioConverter: AVAudioConverter?
    private var audioSourceSignature = ""
    private var loggedFirstVideoPacket = false
    private var loggedFirstAudioPacket = false
    private var loggedAudioFormat = false
    private var loggedVideoFormat = false
    private var notificationObservers: [NSObjectProtocol] = []

    func start() throws {
        guard requestAuthorization(for: .video) else {
            throw CaptureFailure.message("Camera access is not authorized")
        }
        guard requestAuthorization(for: .audio) else {
            throw CaptureFailure.message("Microphone access is not authorized")
        }

        let videoDevice = try exactDevice(
            named: videoDeviceName,
            mediaType: .video,
            deviceTypes: [.external]
        )
        let audioDevice = try exactDevice(
            named: audioDeviceName,
            mediaType: .audio,
            deviceTypes: [.microphone, .external]
        )

        diagnostics.log("Selected video device: \(videoDevice.localizedName) [\(videoDevice.uniqueID)]")
        diagnostics.log("Selected audio device: \(audioDevice.localizedName) [\(audioDevice.uniqueID)]")

        configureVideoFormat(videoDevice)

        let videoInput = try AVCaptureDeviceInput(device: videoDevice)
        let audioInput = try AVCaptureDeviceInput(device: audioDevice)

        session.beginConfiguration()
        do {
            guard session.canAddInput(videoInput) else {
                throw CaptureFailure.message("Cannot add exact video device to capture session")
            }
            session.addInput(videoInput)

            guard session.canAddInput(audioInput) else {
                throw CaptureFailure.message("Cannot add exact audio device to capture session")
            }
            session.addInput(audioInput)

            videoOutput.alwaysDiscardsLateVideoFrames = true
            let preferredPixelFormat: OSType
            if videoOutput.availableVideoPixelFormatTypes.contains(
                kCVPixelFormatType_422YpCbCr8
            ) {
                preferredPixelFormat = kCVPixelFormatType_422YpCbCr8
            } else {
                preferredPixelFormat = kCVPixelFormatType_32BGRA
            }
            videoOutput.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: preferredPixelFormat
            ]
            videoOutput.setSampleBufferDelegate(self, queue: videoQueue)
            guard session.canAddOutput(videoOutput) else {
                throw CaptureFailure.message("Cannot add JPEG video output to capture session")
            }
            session.addOutput(videoOutput)

            audioOutput.setSampleBufferDelegate(self, queue: audioQueue)
            guard session.canAddOutput(audioOutput) else {
                throw CaptureFailure.message("Cannot add PCM audio output to capture session")
            }
            session.addOutput(audioOutput)

            installSessionDiagnostics()
            session.commitConfiguration()
        } catch {
            session.commitConfiguration()
            throw error
        }
        session.startRunning()

        guard session.isRunning else {
            throw CaptureFailure.message("AVCaptureSession did not start")
        }
        diagnostics.log("Capture session running; binary V/A packets are being written to stdout")
    }

    func stop() {
        videoOutput.setSampleBufferDelegate(nil, queue: nil)
        audioOutput.setSampleBufferDelegate(nil, queue: nil)

        if session.isRunning {
            session.stopRunning()
        }

        for observer in notificationObservers {
            NotificationCenter.default.removeObserver(observer)
        }
        notificationObservers.removeAll()
        diagnostics.log("Capture session stopped")
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        autoreleasepool {
            if output === videoOutput {
                emitJPEG(from: sampleBuffer)
            } else if output === audioOutput {
                emitPCM(from: sampleBuffer)
            }
        }
    }

    private func requestAuthorization(for mediaType: AVMediaType) -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: mediaType) {
        case .authorized:
            return true
        case .notDetermined:
            let semaphore = DispatchSemaphore(value: 0)
            var granted = false
            AVCaptureDevice.requestAccess(for: mediaType) { allowed in
                granted = allowed
                semaphore.signal()
            }
            semaphore.wait()
            return granted
        case .denied, .restricted:
            return false
        @unknown default:
            return false
        }
    }

    private func exactDevice(
        named name: String,
        mediaType: AVMediaType,
        deviceTypes: [AVCaptureDevice.DeviceType]
    ) throws -> AVCaptureDevice {
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: deviceTypes,
            mediaType: mediaType,
            position: .unspecified
        )

        // This camera's UVC descriptor is padded with one trailing ASCII space.
        // Strip descriptor padding only; the remaining device name must match exactly.
        let descriptorPadding = CharacterSet.whitespacesAndNewlines
            .union(.controlCharacters)
        guard let device = discovery.devices.first(where: {
            $0.localizedName.trimmingCharacters(in: descriptorPadding) == name
        }) else {
            let discovered = discovery.devices
                .map(\.localizedName)
                .sorted()
                .joined(separator: ", ")
            let listing = discovered.isEmpty ? "none" : discovered
            throw CaptureFailure.message(
                "Required \(mediaType.rawValue) device '\(name)' not found; discovered: \(listing)"
            )
        }
        return device
    }

    private func configureVideoFormat(_ device: AVCaptureDevice) {
        let desiredDimensions = CMVideoDimensions(width: 1280, height: 720)
        let desiredFPS = 30.0

        guard let selection = device.formats.lazy.compactMap({ format -> (
            AVCaptureDevice.Format,
            AVFrameRateRange
        )? in
            let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            guard
                dimensions.width == desiredDimensions.width,
                dimensions.height == desiredDimensions.height,
                let range = format.videoSupportedFrameRateRanges.first(where: {
                    abs($0.maxFrameRate - desiredFPS) < 0.01
                })
            else {
                return nil
            }
            return (format, range)
        }).first else {
            let active = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
            diagnostics.log(
                "1280x720 at 30 fps is unavailable; using bodycam active format "
                    + "\(active.width)x\(active.height) without selecting another device"
            )
            return
        }

        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            device.activeFormat = selection.0
            let frameDuration = selection.1.minFrameDuration
            device.activeVideoMinFrameDuration = frameDuration
            device.activeVideoMaxFrameDuration = frameDuration
            diagnostics.log(
                "Configured bodycam video at 1280x720, "
                    + "\(selection.1.maxFrameRate) fps"
            )
        } catch {
            diagnostics.log("Could not lock bodycam video format: \(error.localizedDescription)")
        }
    }

    private func emitJPEG(from sampleBuffer: CMSampleBuffer) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            diagnostics.log("Video sample did not contain a pixel buffer")
            return
        }

        let image = CIImage(cvPixelBuffer: pixelBuffer)
        guard let jpeg = imageContext.jpegRepresentation(
            of: image,
            colorSpace: jpegColorSpace,
            options: [:]
        ) else {
            diagnostics.log("Failed to encode a video frame as JPEG")
            return
        }

        if !loggedVideoFormat {
            let width = CVPixelBufferGetWidth(pixelBuffer)
            let height = CVPixelBufferGetHeight(pixelBuffer)
            diagnostics.log(
                "Received first video frame: \(width)x\(height), JPEG \(jpeg.count) bytes"
            )
            loggedVideoFormat = true
        }
        packetWriter.write(type: Character("V").asciiValue!, payload: jpeg)
        if !loggedFirstVideoPacket {
            loggedFirstVideoPacket = true
            diagnostics.log("Emitted first JPEG video packet (\(jpeg.count) bytes)")
        }
    }

    private func emitPCM(from sampleBuffer: CMSampleBuffer) {
        do {
            let (inputBuffer, sourceFormat) = try makePCMBuffer(from: sampleBuffer)

            // GENERAL - AUDIO currently exposes exactly 16 kHz mono Float32.
            // AVAudioConverter has returned correctly sized but all-zero output
            // for that already-target-rate format on this device. Convert the
            // samples directly so live speech cannot silently disappear.
            if let directPCM = directTargetPCM(
                from: inputBuffer,
                sourceFormat: sourceFormat
            ) {
                writeAudioPacket(directPCM)
                return
            }

            let signature = audioSignature(sourceFormat)

            if audioConverter == nil || signature != audioSourceSignature {
                guard let converter = AVAudioConverter(from: sourceFormat, to: targetAudioFormat) else {
                    throw CaptureFailure.message(
                        "Cannot convert \(sourceFormat) to mono 16-bit PCM at 16 kHz"
                    )
                }
                audioConverter = converter
                audioSourceSignature = signature
            }

            if !loggedAudioFormat {
                diagnostics.log(
                    "Converting audio from \(sourceFormat.sampleRate) Hz/"
                        + "\(sourceFormat.channelCount) channel(s) to 16000 Hz mono s16le"
                )
                loggedAudioFormat = true
            }

            guard let converter = audioConverter else {
                throw CaptureFailure.message("Audio converter is unavailable")
            }

            let rateRatio = outputSampleRate / sourceFormat.sampleRate
            let outputCapacity = AVAudioFrameCount(
                ceil(Double(inputBuffer.frameLength) * rateRatio) + 64
            )
            guard let outputBuffer = AVAudioPCMBuffer(
                pcmFormat: targetAudioFormat,
                frameCapacity: outputCapacity
            ) else {
                throw CaptureFailure.message("Could not allocate converted audio buffer")
            }

            var suppliedInput = false
            var conversionError: NSError?
            let status = converter.convert(to: outputBuffer, error: &conversionError) {
                _, inputStatus in
                if suppliedInput {
                    inputStatus.pointee = .noDataNow
                    return nil
                }
                suppliedInput = true
                inputStatus.pointee = .haveData
                return inputBuffer
            }

            if status == .error {
                throw conversionError
                    ?? CaptureFailure.message("Unknown AVAudioConverter failure")
            }

            guard outputBuffer.frameLength > 0 else { return }
            let buffers = UnsafeMutableAudioBufferListPointer(outputBuffer.mutableAudioBufferList)
            guard
                let first = buffers.first,
                let bytes = first.mData,
                first.mDataByteSize > 0
            else {
                throw CaptureFailure.message("Converted audio buffer contains no bytes")
            }

            let pcm = Data(bytes: bytes, count: Int(first.mDataByteSize))
            writeAudioPacket(pcm)
        } catch {
            diagnostics.log("Audio sample conversion failed: \(error.localizedDescription)")
        }
    }

    private func directTargetPCM(
        from inputBuffer: AVAudioPCMBuffer,
        sourceFormat: AVAudioFormat
    ) -> Data? {
        guard
            abs(sourceFormat.sampleRate - outputSampleRate) < 0.5,
            sourceFormat.channelCount == 1,
            inputBuffer.frameLength > 0
        else {
            return nil
        }

        let frameCount = Int(inputBuffer.frameLength)
        switch sourceFormat.commonFormat {
        case .pcmFormatFloat32:
            guard let samples = inputBuffer.floatChannelData?[0] else {
                return nil
            }
            var output = [Int16](repeating: 0, count: frameCount)
            for index in 0..<frameCount {
                let sample = samples[index].isFinite ? samples[index] : 0
                let clipped = max(-1.0, min(1.0, sample))
                let scaled = clipped < 0 ? clipped * 32768.0 : clipped * 32767.0
                output[index] = Int16(scaled.rounded())
            }
            return output.withUnsafeBufferPointer { Data(buffer: $0) }

        case .pcmFormatInt16:
            guard let samples = inputBuffer.int16ChannelData?[0] else {
                return nil
            }
            return Data(
                bytes: samples,
                count: frameCount * MemoryLayout<Int16>.size
            )

        default:
            return nil
        }
    }

    private func writeAudioPacket(_ pcm: Data) {
        packetWriter.write(type: Character("A").asciiValue!, payload: pcm)
        if !loggedFirstAudioPacket {
            loggedFirstAudioPacket = true
            diagnostics.log(
                "Emitted first 16 kHz mono s16le audio packet (\(pcm.count) bytes)"
            )
        }
    }

    private func makePCMBuffer(
        from sampleBuffer: CMSampleBuffer
    ) throws -> (AVAudioPCMBuffer, AVAudioFormat) {
        guard
            let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer),
            let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(
                formatDescription
            ),
            let sourceFormat = AVAudioFormat(streamDescription: streamDescription)
        else {
            throw CaptureFailure.message("Audio sample has no usable PCM format description")
        }

        guard sourceFormat.isStandard || sourceFormat.commonFormat != .otherFormat else {
            throw CaptureFailure.message("Audio device did not provide linear PCM")
        }

        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard let pcmBuffer = AVAudioPCMBuffer(
            pcmFormat: sourceFormat,
            frameCapacity: frameCount
        ) else {
            throw CaptureFailure.message("Could not allocate source audio buffer")
        }
        // AVAudioPCMBuffer reports mDataByteSize for its current frameLength,
        // not its frameCapacity. Set the length before asking for destination
        // buffer sizes; otherwise the apparent copy capacity is zero and the
        // resulting live audio is digital silence.
        pcmBuffer.frameLength = frameCount

        let asbd = streamDescription.pointee
        let isNonInterleaved =
            (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
        let bufferCount = isNonInterleaved ? max(1, Int(asbd.mChannelsPerFrame)) : 1
        let audioBufferListSize = MemoryLayout<AudioBufferList>.size
            + max(0, bufferCount - 1) * MemoryLayout<AudioBuffer>.stride
        let storage = UnsafeMutableRawPointer.allocate(
            byteCount: audioBufferListSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { storage.deallocate() }
        let sourceList = storage.bindMemory(to: AudioBufferList.self, capacity: 1)

        var retainedBlockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: sourceList,
            bufferListSize: audioBufferListSize,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &retainedBlockBuffer
        )
        guard status == noErr else {
            throw CaptureFailure.message(
                "CMSampleBuffer audio extraction failed with OSStatus \(status)"
            )
        }

        let sourceBuffers = UnsafeMutableAudioBufferListPointer(sourceList)
        let destinationBuffers = UnsafeMutableAudioBufferListPointer(
            pcmBuffer.mutableAudioBufferList
        )
        guard sourceBuffers.count == destinationBuffers.count else {
            throw CaptureFailure.message(
                "Audio buffer layout mismatch (\(sourceBuffers.count) source, "
                    + "\(destinationBuffers.count) destination)"
            )
        }

        for index in 0..<sourceBuffers.count {
            let source = sourceBuffers[index]
            let destinationCapacity = Int(destinationBuffers[index].mDataByteSize)
            let byteCount = min(Int(source.mDataByteSize), destinationCapacity)
            guard
                let sourceData = source.mData,
                let destinationData = destinationBuffers[index].mData
            else {
                throw CaptureFailure.message("Audio buffer has a nil data pointer")
            }
            memcpy(destinationData, sourceData, byteCount)
            destinationBuffers[index].mDataByteSize = UInt32(byteCount)
        }

        return (pcmBuffer, sourceFormat)
    }

    private func audioSignature(_ format: AVAudioFormat) -> String {
        let asbd = format.streamDescription.pointee
        return [
            String(asbd.mSampleRate),
            String(asbd.mFormatID),
            String(asbd.mFormatFlags),
            String(asbd.mBytesPerPacket),
            String(asbd.mFramesPerPacket),
            String(asbd.mBytesPerFrame),
            String(asbd.mChannelsPerFrame),
            String(asbd.mBitsPerChannel),
        ].joined(separator: ":")
    }

    private func installSessionDiagnostics() {
        let center = NotificationCenter.default

        notificationObservers.append(
            center.addObserver(
                forName: AVCaptureSession.runtimeErrorNotification,
                object: session,
                queue: nil
            ) { notification in
                let error = notification.userInfo?[AVCaptureSessionErrorKey] as? NSError
                diagnostics.log(
                    "Capture runtime error: \(error?.localizedDescription ?? "unknown error")"
                )
            }
        )

        notificationObservers.append(
            center.addObserver(
                forName: AVCaptureSession.wasInterruptedNotification,
                object: session,
                queue: nil
            ) { _ in
                diagnostics.log("Capture session interrupted")
            }
        )

        notificationObservers.append(
            center.addObserver(
                forName: AVCaptureSession.interruptionEndedNotification,
                object: session,
                queue: nil
            ) { _ in
                diagnostics.log("Capture session interruption ended")
            }
        )
    }
}

signal(SIGPIPE, SIG_IGN)

private let capture = BodycamCapture()
do {
    try capture.start()
} catch {
    diagnostics.log("Fatal: \(error.localizedDescription)")
    Darwin.exit(EXIT_FAILURE)
}

private let stopped = DispatchSemaphore(value: 0)
private let signalQueue = DispatchQueue(label: "bodycam.capture.signals")
private let stopLock = NSLock()
private var stopRequested = false

private func stopCapture() {
    stopLock.lock()
    defer { stopLock.unlock() }
    guard !stopRequested else { return }
    stopRequested = true
    capture.stop()
    stopped.signal()
}

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)

private let interruptSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: signalQueue)
interruptSource.setEventHandler {
    stopCapture()
}
interruptSource.resume()

private let terminateSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: signalQueue)
terminateSource.setEventHandler {
    stopCapture()
}
terminateSource.resume()

// If Python is killed before it can signal us, macOS reparents this helper to
// launchd.  An orphaned AVCapture session can keep GENERAL - UVC's video
// interface locked while still allowing a replacement helper to receive audio.
// The parent PID is supplied by server.py; direct standalone use stays valid.
private let expectedParentPID = Int32(
    ProcessInfo.processInfo.environment["BODYCAM_PARENT_PID"] ?? ""
) ?? 0
private let parentWatchdog: DispatchSourceTimer? = {
    guard expectedParentPID > 1 else { return nil }
    let timer = DispatchSource.makeTimerSource(queue: signalQueue)
    timer.schedule(deadline: .now() + .milliseconds(250), repeating: .milliseconds(250))
    timer.setEventHandler {
        if getppid() != expectedParentPID {
            diagnostics.log(
                "Parent process disappeared (expected \(expectedParentPID), now \(getppid())); stopping capture"
            )
            stopCapture()
        }
    }
    timer.resume()
    return timer
}()

stopped.wait()
