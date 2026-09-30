import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
func render(_ size: Int, _ name: String) throws {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let context = NSGraphicsContext.current!.cgContext
    context.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    let base = NSBezierPath(roundedRect: NSRect(x: 56, y: 56, width: 912, height: 912), xRadius: 200, yRadius: 200)
    NSGradient(starting: NSColor(calibratedRed: 0.19, green: 0.51, blue: 0.64, alpha: 1), ending: NSColor(calibratedRed: 0.42, green: 0.76, blue: 0.78, alpha: 1))!.draw(in: base, angle: 65)
    let shine = NSBezierPath(roundedRect: NSRect(x: 64, y: 64, width: 896, height: 896), xRadius: 194, yRadius: 194)
    NSColor.white.withAlphaComponent(0.5).setStroke(); shine.lineWidth = 3; shine.stroke()
    let folder = NSBezierPath()
    folder.move(to: NSPoint(x: 215, y: 348)); folder.line(to: NSPoint(x: 215, y: 659))
    folder.curve(to: NSPoint(x: 255, y: 699), controlPoint1: NSPoint(x: 215, y: 685), controlPoint2: NSPoint(x: 227, y: 699))
    folder.line(to: NSPoint(x: 409, y: 699)); folder.line(to: NSPoint(x: 466, y: 648)); folder.line(to: NSPoint(x: 765, y: 648))
    folder.curve(to: NSPoint(x: 809, y: 604), controlPoint1: NSPoint(x: 795, y: 648), controlPoint2: NSPoint(x: 809, y: 634))
    folder.line(to: NSPoint(x: 809, y: 348)); folder.curve(to: NSPoint(x: 765, y: 304), controlPoint1: NSPoint(x: 809, y: 318), controlPoint2: NSPoint(x: 795, y: 304))
    folder.line(to: NSPoint(x: 259, y: 304)); folder.curve(to: NSPoint(x: 215, y: 348), controlPoint1: NSPoint(x: 229, y: 304), controlPoint2: NSPoint(x: 215, y: 318)); folder.close()
    NSGradient(starting: NSColor.white.withAlphaComponent(0.25), ending: NSColor.white.withAlphaComponent(0.7))!.draw(in: folder, angle: 90)
    NSColor.white.withAlphaComponent(0.75).setStroke(); folder.lineWidth = 5; folder.stroke()
    let arrows = NSBezierPath(); arrows.lineWidth = 22; arrows.lineCapStyle = .round; arrows.lineJoinStyle = .round
    arrows.move(to: NSPoint(x: 353, y: 539)); arrows.line(to: NSPoint(x: 673, y: 539)); arrows.line(to: NSPoint(x: 613, y: 596))
    arrows.move(to: NSPoint(x: 673, y: 539)); arrows.line(to: NSPoint(x: 613, y: 482))
    arrows.move(to: NSPoint(x: 673, y: 420)); arrows.line(to: NSPoint(x: 353, y: 420)); arrows.line(to: NSPoint(x: 413, y: 477))
    arrows.move(to: NSPoint(x: 353, y: 420)); arrows.line(to: NSPoint(x: 413, y: 363))
    NSColor(calibratedRed: 0.1, green: 0.4, blue: 0.52, alpha: 0.9).setStroke(); arrows.stroke()
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name))
}
for size in [16, 32, 128, 256, 512] {
    try render(size, "icon_\(size)x\(size).png")
    try render(size * 2, "icon_\(size)x\(size)@2x.png")
}
