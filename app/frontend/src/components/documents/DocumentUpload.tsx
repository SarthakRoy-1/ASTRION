"use client";

import { useRef, useState, DragEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { uploadDocument } from "@/lib/documents-client";
import { ApiError } from "@/lib/client";

import styles from "./DocumentUpload.module.css";

interface DocumentUploadProps {
  onCancel: () => void;
  onSuccess: () => void;
}

export function DocumentUpload({ onCancel, onSuccess }: DocumentUploadProps) {
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrag = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true);
    } else if (e.type === "dragleave") {
      setDragActive(false);
    }
  };

  const processFile = async (file: File) => {
    if (file.type !== "application/pdf") {
      setError("Only PDF files are supported.");
      return;
    }
    
    setUploading(true);
    setError(null);
    try {
      await uploadDocument(file);
      onSuccess();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError(String(err));
      }
      setUploading(false);
    }
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      void processFile(e.dataTransfer.files[0]);
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault();
    if (e.target.files && e.target.files[0]) {
      void processFile(e.target.files[0]);
    }
  };

  return (
    <div>
      {error && (
        <div style={{ marginBottom: "1rem" }}>
          <Callout tone="fail" title="Upload Failed">
            {error}
          </Callout>
        </div>
      )}
      
      <div 
        className={`${styles.uploadBox} ${dragActive ? styles.active : ""}`}
        onDragEnter={handleDrag}
        onDragLeave={handleDrag}
        onDragOver={handleDrag}
        onDrop={handleDrop}
      >
        <svg 
          className={styles.icon} 
          fill="none" 
          stroke="currentColor" 
          viewBox="0 0 24 24" 
          xmlns="http://www.w3.org/2000/svg"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
        </svg>
        <h3 className={styles.title}>Upload a Document</h3>
        <p className={styles.description}>
          Drag and drop your PDF here, or click to browse.
        </p>
        
        <input 
          ref={inputRef}
          type="file" 
          accept="application/pdf"
          onChange={handleChange}
          className={styles.hiddenInput}
        />
        
        <div className={styles.actions}>
          <Button variant="secondary" onClick={onCancel} disabled={uploading}>
            Cancel
          </Button>
          <Button 
            onClick={() => inputRef.current?.click()} 
            disabled={uploading}
          >
            {uploading ? "Uploading..." : "Select PDF"}
          </Button>
        </div>
      </div>
    </div>
  );
}
